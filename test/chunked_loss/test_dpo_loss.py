from test.utils import HFAlignmentLoss, assert_verbose_allclose, set_seed

import pytest
import torch
import torch.nn.functional as F

from liger_kernel.chunked_loss import LigerFusedLinearDPOLoss
from liger_kernel.chunked_loss.dpo_loss import LigerFusedLinearDPOFunction
from liger_kernel.chunked_loss.functional import liger_fused_linear_dpo

# set random seed globally
set_seed()


class HFDPOLoss(HFAlignmentLoss):
    """
    Implementation of the Odds Ratio Preference Optimization (ORPO) loss,
    adapted from Hugging Face's implementation.
    Reference: https://github.com/huggingface/trl/blob/main/trl/trainer/orpo_trainer.py
    """

    def __init__(self, ignore_index: int = -100, beta: float = 0.1):
        super().__init__(beta=beta, ignore_index=ignore_index)

    def alignment_loss(
        self,
        policy_chosen_logps: torch.FloatTensor,
        policy_rejected_logps: torch.FloatTensor,
    ):
        """Compute DPO loss for a batch of policy log probabilities.
        Args:
            policy_chosen_logps: Log probabilities of the policy model for the chosen responses. Shape: (batch_size,)
            policy_rejected_logps: Log probabilities of the policy model for the rejected responses. Shape: (batch_size,)

        Returns:
            The losses tensor contains the DPO loss for each example in the batch.
        """
        # Derived from https://huggingface.co/papers/2305.18290
        logits_diff = self.beta * (policy_chosen_logps - policy_rejected_logps)
        losses = -F.logsigmoid(logits_diff)
        return losses


class TorchLMHeadDPO(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ignore_index: int = -100,
        beta: float = 0.1,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=bias, dtype=dtype
        )
        self.dpo_loss = HFDPOLoss(
            ignore_index=ignore_index, beta=beta
        ).get_batch_loss_metrics

    def forward(self, x, y):
        return self.dpo_loss(self.lin.weight, x, y, self.lin.bias)


class LigerLMHeadDPO(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ignore_index: int = -100,
        beta: float = 0.1,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=bias, dtype=dtype
        )
        self.dpo_loss = LigerFusedLinearDPOLoss(ignore_index=ignore_index, beta=beta)

    def forward(self, x, y):
        return self.dpo_loss(self.lin.weight, x, y, self.lin.bias)


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (8, 128, 1024, 4096),
        (3, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "scalar, dtype, atol, rtol",
    [
        (1.0, torch.bfloat16, 5e-2, 5e-1),
        (1.0, torch.float32, 2e-2, 5e-1),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
@pytest.mark.parametrize("ignore_index, beta", [(-100, 0.1), (42, 0.2)])
def test_correctness(B, T, H, V, scalar, dtype, atol, rtol, bias, ignore_index, beta):
    B = 2 * B  # dpo loss requires B to be even

    torch_lm_head_dpo = TorchLMHeadDPO(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ignore_index=ignore_index,
        beta=beta,
    )
    liger_lm_head_dpo = LigerLMHeadDPO(
        H=H,
        V=V,
        dtype=dtype,
        bias=bias,
        ignore_index=ignore_index,
        beta=beta,
    )

    torch_lm_head_dpo.lin.weight.data = liger_lm_head_dpo.lin.weight.data = torch.randn(
        V, H, device="cuda", dtype=dtype
    )

    if bias:
        torch_lm_head_dpo.lin.bias.data = liger_lm_head_dpo.lin.bias.data = torch.randn(
            V, device="cuda", dtype=dtype
        )

    _input = torch.randn(B, T, H, device="cuda", dtype=dtype) * scalar
    input1 = _input.detach().clone().requires_grad_(True)
    input2 = _input.detach().clone().requires_grad_(True)

    target = torch.randint(
        0,
        V,
        (
            B,
            T,
        ),
        device="cuda",
        dtype=torch.long,
    )
    # Assign some random number of elements as ignore_index
    num_elements_to_assign = torch.randint(1, B * T // 2, (1,)).item()
    indices_to_assign = torch.randperm(B * T)[:num_elements_to_assign]
    target.view(-1)[indices_to_assign] = ignore_index

    loss1 = torch_lm_head_dpo(input1, target)
    loss2 = liger_lm_head_dpo(input2, target)

    assert_verbose_allclose(loss1, loss2, atol=atol, rtol=rtol)

    loss1.backward()
    loss2.backward()

    assert_verbose_allclose(input1.grad, input2.grad, atol=atol, rtol=rtol)
    assert_verbose_allclose(
        torch_lm_head_dpo.lin.weight.grad,
        liger_lm_head_dpo.lin.weight.grad,
        atol=atol,
        rtol=rtol,
    )
    if bias:
        assert_verbose_allclose(
            torch_lm_head_dpo.lin.bias.grad,
            liger_lm_head_dpo.lin.bias.grad,
            atol=atol,
            rtol=rtol,
        )


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (2, 2, 8, 8),
        (3, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "scalar, dtype, atol, rtol",
    [
        (1.0, torch.bfloat16, 5e-2, 5e-1),
        (1.0, torch.float32, 1e-5, 5e-4),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
def test_correctness_functional(B, T, H, V, scalar, dtype, atol, rtol, bias):
    B = 2 * B

    _input = torch.randn(B, T, H, device="cuda", dtype=dtype) * scalar
    input1 = _input.detach().clone().requires_grad_(True)
    input2 = _input.detach().clone().requires_grad_(True)

    target = torch.randint(
        0,
        V,
        (
            B,
            T,
        ),
        device="cuda",
        dtype=torch.long,
    )

    _weight = torch.randn(V, H, device="cuda", dtype=dtype)
    weight1 = _weight.detach().clone().requires_grad_(True)
    weight2 = _weight.detach().clone().requires_grad_(True)

    _bias = torch.randn(V, device="cuda", dtype=dtype) if bias else None
    bias1 = _bias.detach().clone().requires_grad_(True) if bias else None
    bias2 = _bias.detach().clone().requires_grad_(True) if bias else None

    loss1 = LigerFusedLinearDPOFunction.apply(input1, weight1, target, bias1)
    loss2 = liger_fused_linear_dpo(input2, weight2, target, bias2)

    assert_verbose_allclose(loss1, loss2, atol=atol, rtol=rtol)

    loss1.backward()
    loss2.backward()

    assert_verbose_allclose(input1.grad, input2.grad, atol=atol, rtol=rtol)
    assert_verbose_allclose(weight1.grad, weight2.grad, atol=atol, rtol=rtol)
    if bias:
        assert_verbose_allclose(bias1.grad, bias2.grad, atol=atol, rtol=rtol)