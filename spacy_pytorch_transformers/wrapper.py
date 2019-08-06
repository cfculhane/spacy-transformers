from thinc.extra.wrappers import PyTorchWrapper, xp2torch
from pytorch_transformers.optimization import AdamW, WarmupLinearSchedule
import pytorch_transformers as pytt
import torch.autograd
import torch.nn.utils.clip_grad
import torch
from typing import Tuple, Callable, Any
from thinc.neural.optimizers import Optimizer

from .util import get_pytt_model, get_pytt_config, Activations
from .util import Array, Dropout

FINE_TUNE = True
CONFIG = {"output_hidden_states": True, "output_attentions": True}


class PyTT_Wrapper(PyTorchWrapper):
    """Wrap a PyTorch-Transformers model for use in Thinc."""

    _model: Any
    _optimizer: Any
    _lr_schedule: Any
    cfg: dict

    @classmethod
    def from_pretrained(cls, name):
        config_cls = get_pytt_config(name)
        model_cls = get_pytt_model(name)
        config = config_cls.from_pretrained(name)
        model = model_cls.from_pretrained(name, **CONFIG)
        self = cls(name, config.to_dict(), model)
        self.cfg.update(self.pytt_model.config.to_dict())
        return self

    def __init__(self, name, config, model):
        PyTorchWrapper.__init__(self, model)
        self.cfg = dict(config)

    @property
    def nO(self):
        if "hidden_size" in self.cfg:
            # BERT
            return self.cfg["hidden_size"]
        elif "n_embd" in self.cfg:
            # GPT2
            return self.cfg["n_embd"]
        elif "d_model" in self.cfg:
            # XLNet
            return self.cfg["d_model"]
        elif hasattr(self.pytt_model, "dim"):
            # XLM
            return self.pytt_model.dim
        else:
            keys = ", ".join(self.cfg.keys())
            raise ValueError(f"Unexpected config. Keys: {keys}")

    @property
    def pytt_model(self):
        return self._model

    @property
    def max_length(self):
        return self.cfg["max_position_embeddings"]

    def predict(self, ids: Array):
        ids = torch.as_tensor(ids, dtype=torch.int64)
        model_kwargs = self.get_model_kwargs(ids)
        self._model.eval()
        with torch.no_grad():
            y_var = self._model(ids, **model_kwargs)
        return Activations.from_pytt(y_var, is_grad=False)

    def begin_update(
        self, ids: Array, drop: Dropout = 0.0
    ) -> Tuple[Activations, Callable[..., None]]:
        if drop is None:
            # "drop is None" indicates prediction. It's one of the parts of
            # Thinc's API I'm least happy with...
            return self.predict(ids), lambda dY, sgd=None: None
        ids = torch.as_tensor(ids, dtype=torch.int64)
        model_kwargs = self.get_model_kwargs(ids)
        self._model.train()
        y_var = self._model(ids, **model_kwargs)
        self._model.training = is_training
        output = Activations.from_pytt(y_var, is_grad=False)
        assert output.lh is not None

        def backward_pytorch(d_output: Activations, sgd: Optimizer = None) -> None:
            y_for_bwd = []
            dy_for_bwd = []
            if d_output.has_lh:
                dy_for_bwd.append(xp2torch(d_output.lh))
                y_for_bwd.append(y_var[0])
            if d_output.has_po:
                dy_for_bwd.append(xp2torch(d_output.po))
                y_for_bwd.append(y_var[1])
            if d_output.has_ah:
                raise ValueError("Gradients on all hidden states not supported yet.")
            if d_output.has_aa:
                raise ValueError("Gradients on all attentions not supported yet.")
            if FINE_TUNE:
                torch.autograd.backward(y_for_bwd, grad_tensors=dy_for_bwd)
                if sgd is not None:
                    if self._optimizer is None:
                        self._optimizer = self._create_optimizer(sgd)

                    if getattr(self, "_lr_schedule", None) is None:
                        self._lr_schedule = WarmupLinearSchedule(self._optimizer,
                            warmup_steps=50, t_total=500)
                    if sgd.max_grad_norm:
                        torch.nn.utils.clip_grad.clip_grad_norm_(
                            self._model.parameters(),
                            sgd.max_grad_norm 
                        )
                    optimizer = self._optimizer
                    self._lr_schedule.step()
                    optimizer.step()
                    optimizer.zero_grad()
            return None

        self._model.eval()
        return output, backward_pytorch

    def get_model_kwargs(self, ids):
        # Calculate "attention mask" for BERT and  XLNet, but not GPT2 (sigh)
        if isinstance(self._model, (pytt.BertModel, pytt.XLNetModel)):
            mask = ids.clamp(0, 1)
            segment_ids = torch.zeros_like(ids)
            return {"attention_mask": mask, "token_type_ids": segment_ids}
        else:
            return {}

    def _create_optimizer(self, sgd):
        optimizer = AdamW(
            self._model.parameters(),
            lr=sgd.alpha,
            betas=(sgd.b1, sgd.b2),
        )

        return optimizer
