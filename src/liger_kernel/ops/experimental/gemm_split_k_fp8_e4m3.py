# adapted from: https://github.com/pytorch-labs/applied-ai/blob/main/kernels/triton/inference/fp8/splitk_gemm_fp8.py
# Valid Architectures: [SM_89, SM_90(a)]
import torch
import triton
import triton.language as tl

from liger_kernel.ops.utils import (
    calculate_gemm_settings,
    check_compute_capability_for_fp8,
    ensure_contiguous,
)


@triton.jit
def grouped_launch(
    pid,
    m,  # rows
    n,  # cols
    block_m: tl.constexpr,  # rows in a block
    block_n: tl.constexpr,  # cols in a block
    group_m: tl.constexpr,  # blocks in group along row dimension
):
    grid_m = tl.cdiv(m, block_m)
    grid_n = tl.cdiv(n, block_n)
    width = group_m * grid_n
    group_id = pid // width
    pid_in_group = pid % width
    group_size = tl.minimum(grid_m - group_id * group_m, group_m)
    pid_m = group_id * group_m + (pid_in_group % group_size)
    pid_n = pid_in_group // group_size

    return pid_m, pid_n


@triton.jit
def gemm_split_k_kernel_forward(
    a_ptr,
    b_ptr,
    c_ptr,
    stride_am,
    stride_ak,
    stride_bk,
    stride_bn,
    stride_cm,
    stride_cn,
    m,
    n,
    k,
    block_m: tl.constexpr,
    block_n: tl.constexpr,
    block_k: tl.constexpr,
    split_k: tl.constexpr,
    group_m: tl.constexpr,
):
    pid = tl.program_id(0)
    pid_k = tl.program_id(1)
    grid_k = tl.cdiv(k, block_k * split_k)

    pid_m, pid_n = grouped_launch(pid, m, n, block_m, block_n, group_m)

    offs_m = tl.multiple_of(pid_m * block_m + tl.arange(0, block_m), block_m)
    offs_n = tl.multiple_of(pid_n * block_n + tl.arange(0, block_n), block_n)
    offs_k = pid_k * block_k + tl.arange(0, block_k)

    offs_am = tl.max_contiguous(tl.multiple_of(offs_m, block_m), block_m)
    offs_bn = tl.max_contiguous(tl.multiple_of(offs_n, block_n), block_n)

    a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
    b_ptrs = b_ptr + (offs_k[:, None] * stride_bk + offs_bn[None, :] * stride_bn)

    acc = tl.zeros((block_m, block_n), dtype=tl.float32)

    for k_ in range(0, grid_k, step=2):
        k_remaining = k - k_ * (block_k * split_k)

        mask_a = offs_k[None, :] < k_remaining
        mask_b = offs_k[:, None] < k_remaining

        a = tl.load(a_ptrs, mask=mask_a, other=0.0)
        b = tl.load(b_ptrs, mask=mask_b, other=0.0)

        # fp8 input dot product (supported types: [fp8e4nv, fp8e5, fp8e4b15])
        acc = tl.dot(a, b, acc)

        a_ptrs += block_k * split_k * stride_ak
        b_ptrs += block_k * split_k * stride_bk

    # cast to fp16 pre-store
    acc = acc.to(tl.float16)

    offs_m = pid_m * block_m + tl.arange(0, block_m)
    offs_n = pid_n * block_n + tl.arange(0, block_n)

    c_ptrs = c_ptr + (offs_m[:, None] * stride_cm + offs_n[None, :] * stride_cn)
    mask = (offs_m < m)[:, None] & (offs_n < n)[None, :]

    tl.atomic_add(c_ptrs, acc, mask=mask)


def forward_kernel(ctx, a, b):
    ctx.save_for_backward(a, b)

    # cast to FP8
    # structure:
    #   | 1 bit sign | 4 bit exponent | 3 bit mantissa |
    a, b = a.to(torch.float8_e4m3fn), b.to(torch.float8_e4m3fn)

    m, k = a.shape
    _, n = b.shape

    block_m, block_n, block_k, num_stages, num_warps, split_k, group_m = (
        calculate_gemm_settings(m, n, k)
    )

    total_blocks_m = triton.cdiv(m, block_m)
    total_blocks_n = triton.cdiv(n, block_n)
    total_programs_mn = total_blocks_m * total_blocks_n
    total_programs_k = split_k

    grid = (total_programs_mn, total_programs_k)

    c = torch.zeros((m, n), device=a.device, dtype=torch.float16)
    gemm_split_k_kernel_forward[grid](
        a,
        b,
        c,
        a.stride(0),
        a.stride(1),
        b.stride(0),
        b.stride(1),
        c.stride(0),
        c.stride(1),
        m,
        n,
        k,
        block_m,
        block_n,
        block_k,
        split_k,
        group_m,
        num_stages=num_stages,
        num_warps=num_warps,
    )

    return c


@triton.jit
def gemm_split_k_kernel_backward(
    grad_output_ptr,
    a_ptr,
    b_ptr,
    grad_a_ptr,
    grad_b_ptr,
    stride_gom,
    stride_gon,
    stride_am,
    stride_ak,
    stride_bk,
    stride_bn,
    stride_gam,
    stride_gak,
    stride_gbm,
    stride_gbn,
    m,
    n,
    k,
    block_m: tl.constexpr,
    block_n: tl.constexpr,
    block_k: tl.constexpr,
    split_k: tl.constexpr,
    group_m: tl.constexpr,
):
    pid = tl.program_id(0)
    pid_k = tl.program_id(1)
    grid_k = tl.cdiv(k, block_k * split_k)

    pid_m, pid_n = grouped_launch(pid, m, n, block_m, block_n, group_m)

    offs_m = tl.multiple_of(pid_m * block_m + tl.arange(0, block_m), block_m)
    offs_n = tl.multiple_of(pid_n * block_n + tl.arange(0, block_n), block_n)
    offs_k = pid_k * block_k + tl.arange(0, block_k)

    offs_gom = tl.max_contiguous(tl.multiple_of(offs_m, block_m), block_m)
    offs_gon = tl.max_contiguous(tl.multiple_of(offs_n, block_n), block_n)

    grad_output_ptrs = grad_output_ptr + (
        offs_gom[:, None] * stride_gom + offs_gon[None, :] * stride_gon
    )
    a_ptrs = a_ptr + (offs_gom[:, None] * stride_am + offs_k[None, :] * stride_ak)
    b_ptrs = b_ptr + (offs_k[:, None] * stride_bk + offs_gon[None, :] * stride_bn)

    grad_a_acc = tl.zeros((block_m, block_k), dtype=tl.float32)
    grad_b_acc = tl.zeros((block_k, block_n), dtype=tl.float32)

    for k_ in range(0, grid_k, step=2):
        k_remaining = k - k_ * (block_k * split_k)

        mask_a = offs_k[None, :] < k_remaining
        mask_b = offs_k[:, None] < k_remaining

        grad_output = tl.load(grad_output_ptrs, mask=mask_a, other=0.0)
        a = tl.load(a_ptrs, mask=mask_a, other=0.0)
        b = tl.load(b_ptrs, mask=mask_b, other=0.0)

        grad_a_acc += tl.dot(grad_output, b.T)
        grad_b_acc += tl.dot(a.T, grad_output)

        a_ptrs += block_k * split_k * stride_ak
        b_ptrs += block_k * split_k * stride_bk

    grad_a_acc = grad_a_acc.to(tl.float16)
    grad_b_acc = grad_b_acc.to(tl.float16)

    offs_m = pid_m * block_m + tl.arange(0, block_m)
    offs_n = pid_n * block_n + tl.arange(0, block_n)

    grad_a_ptrs = grad_a_ptr + (
        offs_m[:, None] * stride_gam + offs_k[None, :] * stride_gak
    )
    grad_b_ptrs = grad_b_ptr + (
        offs_k[:, None] * stride_gbm + offs_n[None, :] * stride_gbn
    )

    mask_a = (offs_m < m)[:, None] & (offs_k < k)[None, :]
    mask_b = (offs_k < k)[:, None] & (offs_n < n)[None, :]

    tl.atomic_add(grad_a_ptrs, grad_a_acc, mask=mask_a)
    tl.atomic_add(grad_b_ptrs, grad_b_acc, mask=mask_b)


def backward_kernel(ctx, grad_output):
    a, b = ctx.saved_tensors
    grad_a = torch.zeros_like(a, dtype=torch.float16)
    grad_b = torch.zeros_like(b, dtype=torch.float16)

    grad_output_fp16 = grad_output.to(torch.float16)
    a_fp16 = a.to(torch.float16)
    b_fp16 = b.to(torch.float16)

    m, k = a.shape
    _, n = b.shape

    block_m, block_n, block_k, num_stages, num_warps, split_k, group_m = (
        calculate_gemm_settings(m, n, k)
    )

    total_blocks_m = triton.cdiv(m, block_m)
    total_blocks_n = triton.cdiv(n, block_n)
    total_programs_mn = total_blocks_m * total_blocks_n
    total_programs_k = split_k

    grid = (total_programs_mn, total_programs_k)

    gemm_split_k_kernel_backward[grid](
        grad_output_fp16,
        a_fp16,
        b_fp16,
        grad_a,
        grad_b,
        grad_output.stride(0),
        grad_output.stride(1),
        a.stride(0),
        a.stride(1),
        b.stride(0),
        b.stride(1),
        grad_a.stride(0),
        grad_a.stride(1),
        grad_b.stride(0),
        grad_b.stride(1),
        m,
        n,
        k,
        block_m,
        block_n,
        block_k,
        split_k,
        group_m,
        num_stages=num_stages,
        num_warps=num_warps,
    )

    if ctx.needs_input_grad[0]:
        grad_a = grad_a.to(a.dtype)
    if ctx.needs_input_grad[1]:
        grad_b = grad_b.to(b.dtype)

    return grad_a, grad_b


class LigerFP8GemmSplitKFunction(torch.autograd.Function):
    @staticmethod
    @check_compute_capability_for_fp8
    @ensure_contiguous
    def forward(ctx, a, b):
        return forward_kernel(ctx, a, b)

    @staticmethod
    @check_compute_capability_for_fp8
    @ensure_contiguous
    def backward(ctx, grad_output):
        return backward_kernel(ctx, grad_output)