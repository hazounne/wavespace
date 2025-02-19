from config import *
from funcs import *
import torch
import torch.nn as nn
from torch.nn.utils import weight_norm as normalization
import numpy as np
import torch.nn.functional as F
import torchaudio.transforms as T
def concat(a,b):
    return torch.concatenate((a, b), dim=-1)

class Conv1d(nn.Conv1d):
    def __init__(self, *args, **kwargs):
        self._pad = kwargs.get("padding", (0, 0))
        kwargs["padding"] = 0
        super().__init__(*args, **kwargs) # self._pad 값을 받고 에러 안뜨게 padding=0으로 만든 후 Conv1d 상속

    def forward(self, x):
        x = nn.functional.pad(x, self._pad)
        
        out = nn.functional.conv1d(
            x, 
            self.weight,
            self.bias,
            self.stride,
            self.padding,
            self.dilation,
            self.groups,
        )
        #if not TRAINING:
        #    print(f'Conv {x.shape[1:]} -> {out.shape[1:]}') # console_check
        return out

class TrConv1d(nn.ConvTranspose1d):
    def __init__(self, *args, **kwargs):
        self._pad = kwargs.get("padding", (0, 0))
        kwargs["padding"] = 0
        super().__init__(*args, **kwargs) # self._pad 값을 받고 에러 안뜨게 padding=0으로 만든 후 Conv1d 상속

    def forward(self, x):
        x = nn.functional.pad(x, self._pad)
        out = torch.nn.functional.conv_transpose1d(
            x,
            self.weight,
            self.bias,
            self.stride,
            self.padding,
            self.output_padding,
            self.groups,
            self.dilation,
        )
        #if not TRAINING:
        #    print(f'TrConv {x.shape[1:]} -> {out.shape[1:]}') # console_check
        return out

class UpSampleBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride):
        super().__init__()

        relu = nn.LeakyReLU(0.2)
        self.net = nn.Sequential(relu, TrConv1d(in_channels=in_channels, 
                                                out_channels = out_channels, 
                                                kernel_size = kernel_size, 
                                                stride = stride))

    def forward(self, x):
        out = self.net(x)
        # if not TRAINING:
        #     print(f'{x.size()} -> {out.size()}') # architectural_dimensional_fidelity_check
        return out

class ResBlock(nn.Module):
    def __init__(self, channels):
        super().__init__()

        relu = nn.LeakyReLU(0.2)

        self.net = nn.Sequential(relu, nn.Conv1d(channels, channels, kernel_size=1, stride=1))

    def forward(self, x):
        return x + self.net(x)


##################################
# Defines Each part of the model.
##################################
'''
class Block(nn.Module):
    def __init__(
                self,
            ):
        super().__init__()

        net = []
        self.net = nn.Sequential(*net)

    def forward(self, x):
        return self.net(x)
'''
# class N_Encoder(nn.Module):
#     def __init__(
#                 self,
#             ):
#         super().__init__()

#         net = []
#         net.append(nn.Linear(X_DIM,1))
#         self.net = nn.Sequential(*net)

#     def forward(self, x):
#         return self.net(x)

class Encoder(nn.Module):
    def __init__(
                self,
            ):
        super().__init__()
        relu = nn.LeakyReLU(0.2)

        if BLOCK_STYLE == 'CONV1D':
            net = []
            for i in range(len(ENC_H)-1):
                net.extend([Conv1d(ENC_H[i],
                                   ENC_H[i+1],
                                   kernel_size=ENC_K[i],
                                   stride=ENC_S[i],
                                   padding=get_padding(ENC_K[i], ENC_S[i])),
                            relu, nn.BatchNorm1d(ENC_H[i+1])
                ])
            self.net = nn.Sequential(*net)
            self.linear = nn.Sequential(nn.Linear(ENC_H[-1], LATENT_LEN*2), relu)
    def forward(self, x):
        if BLOCK_STYLE == 'CONV1D':
            #preprocessing: dc offset to 0, normalise energy.
            x_mag = torch.cat((torch.zeros(x.shape[0],1).to(DEVICE),dft(x)[:,1:]), dim=-1)
            preprocessed_x = idft(x_mag, cos=False)
            x_spec = x_mag.abs()
            amp = torch.sum(preprocessed_x.pow(2), dim=-1).sqrt().unsqueeze(-1)
            preprocessed_x = preprocessed_x / amp
            x_spec = x_spec / amp

            #encoder
            preprocessed_x = preprocessed_x * NORMALISED_ENERGY
            conv1d_passed_preprocessed_x = self.net(preprocessed_x.unsqueeze(1))
            conv1d_passed_preprocessed_x = torch.squeeze(conv1d_passed_preprocessed_x, dim=2)
            w = self.linear(conv1d_passed_preprocessed_x)
            return w[:, :W_DIM], w[:, W_DIM:2*W_DIM], preprocessed_x, x_spec

class Decoder(nn.Module):
    def __init__(
                self,
            ):
        super().__init__()
        relu = nn.LeakyReLU(0.2)
        tanh = nn.Tanh()
        relu_out = nn.ReLU()

        if BLOCK_STYLE == 'CONV1D':
            self.linear = nn.Sequential(nn.Linear(LATENT_LEN+SEMANTIC_CONDITION_LEN, DEC_H[0]), relu)
            if not AB_S:
                self.output_dense = nn.Sequential(Conv1d(DEC_H[-1], 1, kernel_size=1, stride=1), tanh)
            else:
                self.amp_dense = nn.Sequential(Conv1d(DEC_H[-1], 1, kernel_size=2, stride=2), relu) #we may take negative values to spin clockwise but we followed the practical way of additive synthesizers which only uses 0 and positive values.
                self.phase_dense = nn.Sequential(Conv1d(DEC_H[-1], 1, kernel_size=2, stride=2), tanh)
            net = []
            for i in range(len(DEC_H)-1):
                net.extend([UpSampleBlock(DEC_H[i], DEC_H[i+1], kernel_size=DEC_K[i], stride=DEC_S[i])])
                for j in range(RES_BLOCK_CONV_NUM):
                    net.extend([ResBlock(DEC_H[i+1])])
            self.net = nn.Sequential(*net)

    def forward(self, x):
        if BLOCK_STYLE == 'CONV1D':
            x = self.linear(x).unsqueeze(2)
            x = self.net(x)
            if not AB_S:
                out = self.output_dense(x).squeeze(1)
                magnitude = dft(out)
                magnitude[:,0] = 0
                
            else:
                amp = self.amp_dense(x).squeeze(1)
                phase = self.phase_dense(x).squeeze(1)
                magnitude = torch.concat((torch.zeros(x.shape[0],1).to(DEVICE), amp*torch.exp(1j * phase)), dim=-1)
            x_hat = idft(magnitude)
            amp_overall = torch.sum(x_hat.pow(2), dim=-1).sqrt().unsqueeze(-1)
            x_hat = x_hat/amp_overall
            x_hat = x_hat*NORMALISED_ENERGY
            amplitude = magnitude.abs()/amp_overall
            magnitude = magnitude/amp_overall
            return x_hat, amplitude

class CONV1D(nn.Module):
    def __init__(
                self,
            ):
        super().__init__()

        self.unit = UNIT()
        self.rep = 3
    def forward(self, x):
        for _ in range(self.rep): x = self.unit(x)
        return x
    
class UNIT(nn.Module):
    def __init__(
                self,
            ):
        super().__init__()

        net = []
        net.extend([normalization(nn.Linear(W_DIM,W_DIM)),
                    nn.ReLU()])
        self.net = nn.Sequential(*net)

    def forward(self, x):
        return self.net(x)