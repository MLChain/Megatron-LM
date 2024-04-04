import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from megatron.core.transformer.module import MegatronModule
from einops import rearrange, repeat
from megatron.core.transformer.transformer_config import TransformerConfig
from megatron.core.tensor_parallel import (
    get_cuda_rng_tracker,
    ColumnParallelLinear,
    RowParallelLinear,
    copy_to_tensor_model_parallel_region,
    reduce_from_tensor_model_parallel_region,
    reduce_scatter_to_sequence_parallel_region,
    gather_from_sequence_parallel_region,
)

from megatron.core.parallel_state import (
      get_global_memory_buffer,
      get_tensor_model_parallel_group,
      get_tensor_model_parallel_rank,
      get_tensor_model_parallel_world_size,
)
from mamba_ssm.ops.selective_scan_interface import selective_scan_fn

try:
    from mamba_ssm.ops.triton.selective_state_update import selective_state_update
except ImportError:
    selective_state_update = None

try:
    from causal_conv1d import causal_conv1d_fn, causal_conv1d_update
except ImportError:
    causal_conv1d_fn = None
    causal_conv1d_update = None

# from megatron import print_rank_0
from src.ops.triton.flashmamba import mamba_chunk_scan_fused
from src.ops.triton.layernorm_gated import RMSNorm as RMSNormGated

class Mamba(MegatronModule):
    def __init__(
        self,
        config: TransformerConfig,
        d_model,
        d_state=128,
        d_conv=4,
        conv_init=None,
        expand=2,
        headdim=64,
        ngroups=1,
        A_init_range=(1, 16),
        D_has_hdim=False,
        rmsnorm=True,
        norm_before_gate=False,
        dt_min=0.001,
        dt_max=0.1,
        dt_init="random",
        dt_scale=1.0,
        dt_init_floor=1e-4,
        bias=False,
        conv_bias=True,
        # Fused kernel and sharding options
        chunk_size=128,
        use_fast_path=True,
        layer_idx=None,
        device=None,
        dtype=None,
    ):
        factory_kwargs = {"device": torch.cuda.current_device(), "dtype": config.params_dtype}
        super().__init__(config)
        self.config = config
        self.d_model = d_model
        self.d_state = d_state
        self.d_conv = d_conv
        self.conv_init = conv_init
        self.expand = expand
        self.d_inner = int(self.expand * self.d_model)
        self.headdim = headdim
        self.ngroups = ngroups
        assert self.d_inner % self.headdim == 0
        self.nheads = self.d_inner // self.headdim
        self.D_has_hdim = D_has_hdim
        self.rmsnorm = rmsnorm
        self.norm_before_gate = norm_before_gate
        self.chunk_size = chunk_size
        self.use_fast_path = use_fast_path
        self.layer_idx = layer_idx

        self.tensor_model_parallel_size = get_tensor_model_parallel_world_size()
        assert (self.d_inner % self.tensor_model_parallel_size == 0)
        assert (self.ngroups % self.tensor_model_parallel_size == 0)
        assert (self.nheads % self.tensor_model_parallel_size == 0)
        assert (not bias)

        self.d_inner_local = self.d_inner // self.tensor_model_parallel_size
        self.ngroups_local = self.ngroups // self.tensor_model_parallel_size
        self.nheads_local = self.nheads // self.tensor_model_parallel_size

        #self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=bias, **factory_kwargs)
        #assume sequence parallelism; input is already partitioned along sequence dimension
        self.in_proj = ColumnParallelLinear(
            self.d_model,
            self.d_inner * 2 + 2 * self.ngroups * self.d_state + self.nheads,
            config=self.config,
            init_method=self.config.init_method,
            gather_output=False,
            bias=bias,
        )

        conv_dim = self.d_inner_local + 2 * self.ngroups_local * self.d_state
        with get_cuda_rng_tracker().fork():
            self.conv1d = nn.Conv1d(
                in_channels=conv_dim,
                out_channels=conv_dim,
                bias=conv_bias,
                kernel_size=d_conv,
                groups=conv_dim,
                padding=d_conv - 1,
                device=torch.cuda.current_device(),
                dtype=config.params_dtype
            )
            setattr(self.conv1d.weight, 'tensor_model_parallel', True)
            setattr(self.conv1d.bias, 'tensor_model_parallel', True)

            if self.conv_init is not None:
                nn.init.uniform_(self.conv1d.weight, -self.conv_init, self.conv_init)
            # self.conv1d.weight._no_weight_decay = True

        self.activation = "silu"
        self.act = nn.SiLU()

        with get_cuda_rng_tracker().fork():
            # Initialize dt bias so that F.softplus(dt_bias) is between dt_min and dt_max
            dt = torch.exp(
                torch.rand(self.nheads_local, **factory_kwargs) * (math.log(dt_max) - math.log(dt_min))
                + math.log(dt_min)
            ).clamp(min=dt_init_floor)
            # Inverse of softplus: https://github.com/pytorch/pytorch/issues/72759
            inv_dt = dt + torch.log(-torch.expm1(-dt))
            with torch.no_grad():
                self.dt_bias = nn.Parameter(inv_dt)
            # Our initialization would set all Linear.bias to zero, need to mark this one as _no_reinit
            self.dt_bias._no_reinit = True
            # Just to be explicit. Without this we already don't put wd on dt_bias because of the check
            # name.endswith("bias") in param_grouping.py
            self.dt_bias._no_weight_decay = True

            assert A_init_range[0] > 0 and A_init_range[1] >= A_init_range[0]
            A = torch.empty(self.nheads_local, dtype=torch.float32, device=torch.cuda.current_device()).uniform_(*A_init_range)
            A_log = torch.log(A)  # Keep A_log in fp32
            self.A_log = nn.Parameter(A_log)
            self.A_log._no_weight_decay = True
            setattr(self.A_log, 'tensor_model_parallel', True)

        # D "skip" parameter
        self.D = nn.Parameter(torch.ones(self.d_inner_local if self.D_has_hdim else self.nheads_local,
                                         device=torch.cuda.current_device()))  # Keep in fp32
        self.D._no_weight_decay = True
        setattr(self.D, 'tensor_model_parallel', True)

        if self.rmsnorm:
            # TODO (rwaleffe): norm should be tp independent with group_size = d_inner_local / ngroups_local
            assert RMSNormGated is not None
            self.norm = RMSNormGated(self.d_inner_local, eps=1e-5, norm_before_gate=False, **factory_kwargs)

        #self.out_proj = nn.Linear(self.d_inner, self.d_model, bias=bias, **factory_kwargs)
        # assume sequence parallelism: input is partitioned along d_innear and output is partitioned along sequence dimension
        self.out_proj = RowParallelLinear( 
            self.d_inner,
            self.d_model,
            config=self.config,
            init_method=self.config.output_layer_init_method,
            bias=bias,
            input_is_parallel=True,
            skip_bias_add=False,
        )

    def forward(self, hidden_states, inference_params=None):
        """
        hidden_states: (nL, B, D) / (L B D)
        Returns: same shape as hidden_states
        """
        _, batch, dim = hidden_states.shape

        # TODO (rwaleffe): need to update inference throughout
        conv_state, ssm_state = None, None
        if inference_params is not None:
            assert not self.config.sequence_parallel
            conv_state, ssm_state = self._get_states_from_cache(inference_params, batch)
            if inference_params.seqlen_offset > 0:
                # The states are updated inplace
                out, _, _ = self.step(hidden_states, conv_state, ssm_state)
                return out

        # (pd, d_state)
        A = -torch.exp(self.A_log.float())

        # pl b d ->  l b p(2d)
        # TODO move transpose to GEMM
        if (self.config.sequence_parallel):
            # gather data along sequenece dimension
            hidden_states = gather_from_sequence_parallel_region(hidden_states)
        else:
            hidden_states = copy_to_tensor_model_parallel_region(hidden_states)
        xz = hidden_states @ self.in_proj.weight.t()

        z, xBC, dt = torch.split(xz, [self.d_inner_local,
                                      self.d_inner_local + 2 * self.ngroups_local * self.d_state,
                                      self.nheads_local], dim=-1)

        # transpose: l b pd --> b pd l
        xBC = rearrange(xBC, "l b d -> b d l")
        xBC = xBC.contiguous()

        # Compute short convolution
        if conv_state is not None:
            # If we just take x[:, :, -self.d_conv :], it will error if seqlen < self.d_conv
            # Instead F.pad will pad with zeros if seqlen < self.d_conv, and truncate otherwise.
            conv_state.copy_(F.pad(x, (self.d_conv - x.shape[-1], 0)))  # Update state (B D W)

        seqlen = xBC.size(2)
        if causal_conv1d_fn is None:
            xBC = self.act(self.conv1d(xBC)[..., :seqlen])
        else:
            assert self.activation in ["silu", "swish"]
            xBC = causal_conv1d_fn(
                x=xBC,
                weight=rearrange(self.conv1d.weight, "d 1 w -> d w"),
                bias=self.conv1d.bias,
                activation=self.activation,
            )

        # transpose b pd l --> l b pd
        xBC = rearrange(xBC, "b d l ->  l b d")
        xBC = xBC.contiguous()

        x, B, C = torch.split(xBC, [self.d_inner_local,
                                    self.ngroups_local * self.d_state, self.ngroups_local * self.d_state], dim=-1)

        # TODO Vijay: fuse most of the transposes with the GEMMS
        x = rearrange(x, "l b (h p) -> b l h p", p=self.headdim).contiguous()
        dt = rearrange(dt, "l b d -> b l d").contiguous()
        B = rearrange(B, "l b (g n) -> b l g n", n=self.d_state).contiguous()
        C = rearrange(C, "l b (g n) -> b l g n", n=self.d_state).contiguous()
        z = rearrange(z, "l b (h p) -> b l h p", p=self.headdim).contiguous()
        y = mamba_chunk_scan_fused(
            x,
            dt,
            A,
            B,
            C,
            self.chunk_size,
            D=rearrange(self.D.float(), "(h p) -> h p", p=self.headdim) if self.D_has_hdim else self.D,
            z=z if not self.rmsnorm else None,
            dt_bias=self.dt_bias.float(),
            dt_softplus=True,
            return_final_states=ssm_state is not None,
        )

        if ssm_state is not None:
            y, last_state = y
            ssm_state.copy_(last_state)

        if self.rmsnorm:
            y = rearrange(y, "b l h p -> b l (h p)").contiguous()
            z = rearrange(z, "b l h p -> b l (h p)").contiguous()
            y = self.norm(y, z)
            y = rearrange(y, "b l d -> l b d").contiguous()
        else:
            y = rearrange(y, "b l h p -> l b (h p)").contiguous()

        #  l b pd --> pl b d
        out_full = y @ self.out_proj.weight.t()
        if (self.config.sequence_parallel):
            out = reduce_scatter_to_sequence_parallel_region(out_full)
        else:
            out = reduce_from_tensor_model_parallel_region(out_full)
        return out

    def selective_scan_ref(self, u, delta, A, B, C, D=None, z=None, delta_bias=None, delta_softplus=False,
                      return_last_state=False):
        """
        u: r(B D L)
        delta: r(B D L)
        A: c(D N) or r(D N)
        B: c(D N) or r(B N L) or r(B N 2L) or r(B G N L) or (B G N L)
        C: c(D N) or r(B N L) or r(B N 2L) or r(B G N L) or (B G N L)
        D: r(D)
        z: r(B D L)
        delta_bias: r(D), fp32

        out: r(B D L)
        last_state (optional): r(B D dstate) or c(B D dstate)

        u: r(L B D)
        delta: r(L B D)
        A: c(D N) or r(D N)
        B: r(L B N)
        C: r(L B N)
        D: r(D)
        z: r(L B D)
        delta_bias: r(D), fp32

        out: r(L B D)
        """
        dtype_in = u.dtype
        u = u.float()
        delta = delta.float()
        #print("delta_shape", delta.size())
        #print("delta_bias_shape", delta_bias.size())
        if delta_bias is not None:
            #delta = delta + delta_bias[..., None].float()
            delta = delta + delta_bias.float()
        if delta_softplus:
            delta = F.softplus(delta)
        batch, dim, dstate = u.shape[1], A.shape[0], A.shape[1]
        B = B.float()
        C = C.float()

        x = A.new_zeros((batch, dim, dstate))
        ys = []
        #deltaA = torch.exp(torch.einsum('lbd,dn->lbdn', delta, A))
        #deltaB_u = torch.einsum('lbd,lbn,lbd->lbdn', delta, B, u)
        last_state = None
        #print("batch, dim, dstate", batch, dim, dstate)
        #temp1 = x.new_empty((batch, dim, dstate))
        #temp2 = x.new_empty((batch, dim, dstate))

        for i in range(u.shape[0]):
            #x = deltaA[i] * x + deltaB_u[i]

            #x = delta[i].unsqueeze(dim=-1) * (A.unsqueeze(dim=0) * x + B[i].unsqueeze(dim=1) * u[i].unsqueeze(dim=-1))
            #temp1 = A.unsqueeze(dim=0) * x
            #temp2 = B[i].unsqueeze(dim=1) * u[i].unsqueeze(dim=-1)
            #temp1 = temp1 + temp2
            #x = delta[i].unsqueeze(dim=-1) * temp1

            y = torch.einsum('bdn,bn->bd', x, C[i])
            if i == u.shape[0] - 1:
                last_state = x
            ys.append(y)
        y = torch.stack(ys)  # (L batch dim)
        out = y if D is None else y + u * D
        if z is not None:
            out = out * F.silu(z)
        out = out.to(dtype=dtype_in)
        return out if not return_last_state else (out, last_state)

    def step(self, hidden_states, conv_state, ssm_state):
        dtype = hidden_states.dtype
        assert hidden_states.shape[0] == 1, "Only support decoding with 1 token at a time for now"

        # l b d --> b d
        hidden_states = hidden_states.squeeze(0)

        #  b d_model --> b p(2d)
        xz = hidden_states @ self.in_proj.weight.t()

        # b p(2d) -->  b pd ; b pd
        x, z = xz.chunk(2, dim=-1)

        # Conv step
        if causal_conv1d_update is None:
            conv_state.copy_(torch.roll(conv_state, shifts=-1, dims=-1))  # Update state (B D W)
            conv_state[:, :, -1] = x
            x = torch.sum(conv_state * rearrange(self.conv1d.weight, "d 1 w -> d w"), dim=-1)  # (B D)
            if self.conv1d.bias is not None:
                x = x + self.conv1d.bias
            x = self.act(x).to(dtype=dtype)
        else:
            x = causal_conv1d_update(
                x,
                conv_state,
                rearrange(self.conv1d.weight, "d 1 w -> d w"),
                self.conv1d.bias,
                self.activation,
            )

        # b pd ---> b d
        x_db = x @ self.x_proj.weight.t()  # (B dt_rank+2*d_state)
        x_db = reduce_from_tensor_model_parallel_region(x_db)

        dt, B, C = torch.split(x_db, [self.dt_rank, self.d_state, self.d_state], dim=-1)
        
        # b dt_rank  --> b pd
        dt = dt @ self.dt_proj.weight.t()
        
        # (pd, d_state)
        A = -torch.exp(self.A_log.float())

        # SSM step
        if selective_state_update is None:
            # Discretize A and B
            dt = F.softplus(dt + self.dt_proj.bias.to(dtype=dt.dtype))
            dA = torch.exp(torch.einsum("bd,dn->bdn", dt, A))
            dB = torch.einsum("bd,bn->bdn", dt, B)
            ssm_state.copy_(ssm_state * dA + rearrange(x, "b d -> b d 1") * dB)
            y = torch.einsum("bdn,bn->bd", ssm_state.to(dtype), C)
            y = y + self.D.to(dtype) * x
            y = y * self.act(z)  # (B D)
        else:
            y = selective_state_update(
                ssm_state, x, dt, A, B, C, self.D, z=z, dt_bias=self.dt_proj.bias, dt_softplus=True
            )

        # b pd --> b d
        out = y @ self.out_proj.weight.t()
        out = reduce_from_tensor_model_parallel_region(out)
        return out.unsqueeze(0), conv_state, ssm_state

    def allocate_inference_cache(self, batch_size, max_seqlen, dtype=None, **kwargs):
        device = self.out_proj.weight.device
        conv_dtype = self.conv1d.weight.dtype if dtype is None else dtype
        conv_state = torch.zeros(
            batch_size, self.d_inner_local, self.d_conv, device=device, dtype=conv_dtype
        )
        ssm_dtype = self.dt_proj.weight.dtype if dtype is None else dtype
        # ssm_dtype = torch.float32
        ssm_state = torch.zeros(
            batch_size, self.d_inner_local, self.d_state, device=device, dtype=ssm_dtype
        )
        return conv_state, ssm_state

    def _get_states_from_cache(self, inference_params, batch_size, initialize_states=False):
        assert self.layer_idx is not None
        if self.layer_idx not in inference_params.key_value_memory_dict:
            conv_state = torch.zeros(
                batch_size,
                self.d_inner_local,
                self.d_conv,
                device=self.conv1d.weight.device,
                dtype=self.conv1d.weight.dtype,
            )
            ssm_state = torch.zeros(
                batch_size,
                self.d_inner_local,
                self.d_state,
                device=self.dt_proj.weight.device,
                dtype=self.dt_proj.weight.dtype,
            )
            inference_params.key_value_memory_dict[self.layer_idx] = (conv_state, ssm_state)
        else:
            conv_state, ssm_state = inference_params.key_value_memory_dict[self.layer_idx]
            # TODO: What if batch size changes between generation, and we reuse the same states?
            if initialize_states:
                conv_state.zero_()
                ssm_state.zero_()
        return conv_state, ssm_state
