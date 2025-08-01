import torch
import torch.nn as nn
from torch import Tensor
from typing import Optional, Tuple
import torch.nn.functional as F

from .config import VapConfig
from ..encoder import EncoderCPC
from ..modules import GPT, GPTStereo
from ..objective import ObjectiveVAP

class VapGPT_nod(nn.Module):
    def __init__(self, conf: Optional[VapConfig] = None):
        super().__init__()
        if conf is None:
            conf = VapConfig()
        self.conf = conf
        self.sample_rate = conf.sample_rate
        self.frame_hz = conf.frame_hz

        self.temp_elapse_time = []

        # Single channel
        self.ar_channel = GPT(
            dim=conf.dim,
            dff_k=3,
            num_layers=conf.channel_layers,
            num_heads=conf.num_heads,
            dropout=conf.dropout,
            context_limit=conf.context_limit,
        )

        # Cross channel
        self.ar = GPTStereo(
            dim=conf.dim,
            dff_k=3,
            num_layers=conf.cross_layers,
            num_heads=conf.num_heads,
            dropout=conf.dropout,
            context_limit=conf.context_limit,
        )

        self.objective = ObjectiveVAP(bin_times=conf.bin_times, frame_hz=conf.frame_hz)

        # Outputs
        # Voice activity objective -> x1, x2 -> logits ->  BCE
        self.va_classifier = nn.Linear(conf.dim, 1)
        
        if self.conf.lid_classify == 1:
            self.lid_classifier = nn.Linear(conf.dim, conf.lid_classify_num_class)
        
        elif self.conf.lid_classify == 2:
            self.lid_classifier_middle = nn.Linear(conf.dim*2, conf.lid_classify_num_class)
        
        if self.conf.lang_cond == 1:
            self.lang_condition = nn.Linear(conf.lid_classify_num_class, conf.dim)
        
        self.vap_head = nn.Linear(conf.dim, self.objective.n_classes)

        # For Nodding
        self.nod_head = nn.Linear(conf.dim, 4)
        self.bc_head = nn.Linear(conf.dim, 1)

    def load_encoder(self, cpc_model):
        
        # Audio Encoder
        self.encoder1 = EncoderCPC(
            load_pretrained=True if self.conf.load_pretrained == 1 else False,
            freeze=self.conf.freeze_encoder,
            cpc_model=cpc_model
        )
        self.encoder1 = self.encoder1.eval()
        
        self.encoder2 = EncoderCPC(
            load_pretrained=True if self.conf.load_pretrained == 1 else False,
            freeze=self.conf.freeze_encoder,
            cpc_model=cpc_model
        )

        self.encoder2 = self.encoder2.eval()
        
        if self.conf.freeze_encoder == 1:
            print('freeze encoder')
            self.encoder1.freeze()
            self.encoder2.freeze()

    @property
    def horizon_time(self):
        return self.objective.horizon_time

    def encode_audio(self, audio1: torch.Tensor, audio2: torch.Tensor) -> Tuple[Tensor, Tensor]:
        
        # Channel swap for temporal consistency
        x1 = self.encoder1(audio2)  # speaker 1 (User)
        x2 = self.encoder2(audio1)  # speaker 2 (System)

        return x1, x2

    def vad_loss(self, vad_output, vad):
        return F.binary_cross_entropy_with_logits(vad_output, vad)
    
    def forward(self, x1: Tensor, x2: Tensor) -> Tuple[Tensor, Tensor, list[Tensor]]:
        """
        Forward pass for the VapGPT model.
        
        Args:
            x1 (Tensor): Input audio embedded tensor for speaker 1.
            x2 (Tensor): Input audio embedded tensor for speaker 2.

        Returns:
            dict: A dictionary containing:
                - p_bc (Tensor): Probability of backchannel.
                - p_nod_short (list[Tensor]): Probability of short nodding.
                - p_nod_long (list[Tensor]): Probability of long nodding.
                - p_nod_long_p (list[Tensor]): Probability of long nodding with preparation.
        """
        # Autoregressive
        o1 = self.ar_channel(x1)  # ["x"]
        o2 = self.ar_channel(x2)  # ["x"]
        out = self.ar(o1["x"], o2["x"])

        p_bc = self.bc_head(out["x"])
        nod = self.nod_head(out["x"])
        
        p_bc = p_bc.sigmoid().to('cpu').tolist()[0][-1][0]
        nod_ = nod.softmax(dim=-1).to('cpu').tolist()[0][-1]
        p_nod_short = nod_[1]
        p_nod_long = nod_[2]
        p_nod_long_p = nod_[3]

        ret = {
            "p_bc": p_bc,
            "p_nod_short": p_nod_short,
            "p_nod_long": p_nod_long,
            "p_nod_long_p": p_nod_long_p
        }

        return ret