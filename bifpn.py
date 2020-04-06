import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import xavier_init
from mmcv.cnn import constant_init, kaiming_init
from torch.nn.modules.batchnorm import _BatchNorm


from mmdet.core import auto_fp16
from ..registry import NECKS
from ..utils import ConvModule

class WeightedMerge(nn.Module):
    def __init__(self, in_channels, out_channels, target_size, norm_cfg, apply_bn=False, eps=0.0001):
        super(WeightedMerge, self).__init__()
        self.conv = SeparableConv2d(out_channels, out_channels, 3, padding=1)
        self.eps = eps
        self.num_ins = len(in_channels)
        self.weight = nn.Parameter(torch.Tensor(self.num_ins).fill_(1))
        self.relu = nn.ReLU(inplace=False)
        self.swish = Swish()
        self.resample_ops = nn.ModuleList()
        for in_c in in_channels:
            self.resample_ops.append(Resample(in_c, out_channels, target_size, norm_cfg, apply_bn))

    def forward(self, inputs):
        assert isinstance(inputs, list)
        assert len(inputs) == self.num_ins
        w = self.relu(self.weight)
        w /= (w.sum() + self.eps)
        x = 0
        for i in range(self.num_ins):
            x += w[i] * self.resample_ops[i](inputs[i])
        output = self.conv(self.swish(x))
        return output


class Resample(nn.Module):
    def __init__(self, in_channels, out_channels, target_size, norm_cfg, apply_bn=False):
        super(Resample, self).__init__()
        self.target_size = torch.Size([target_size, target_size])
        self.conv = ConvModule(in_channels,
                out_channels,
                1,
                norm_cfg=norm_cfg if not apply_bn else None,
                activation=None,
                inplace=False)

    def _resize(self, x, size):
        if x.shape[-2:] == size:
            return x
        elif x.shape[-2:] < size:
            return F.interpolate(x, size=size, mode='nearest')
        else:
            assert x.shape[-2] % size[-2] == 0 and x.shape[-1] % size[-1] == 0
            kernel_size = x.shape[-1] // size[-1]
            x = F.max_pool2d(x, kernel_size=kernel_size+1, stride=kernel_size, padding=1)
            return x

    def forward(self, inputs):
        feat = self.conv(inputs)
        return self._resize(feat, self.target_size)


class SeparableConv2d(nn.Module):
    def __init__(self, in_channels,
                 out_channels,
                 kernel_size=1,
                 stride=1,
                 padding=0,
                 dilation=1,
                 bias=False):
        super(SeparableConv2d, self).__init__()
        self.depthwise = nn.Conv2d(in_channels, in_channels, kernel_size,
                               stride, padding, dilation, groups=in_channels, bias=bias)
        self.pointwise = ConvModule(in_channels, out_channels, 1,
                                    norm_cfg=dict(type='BN', momentum=0.003, eps=1e-4, requires_grad=True), activation=None, inplace=False)

    def forward(self, x):
        x = self.depthwise(x)
        x = self.pointwise(x)
        return x


class Swish(nn.Module):
    def __init__(self):
        super(Swish, self).__init__()

    def forward(self, x):
        x = x * F.sigmoid(x)
        return x


class bifpn_layer(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 target_size_list,
                 num_outs=5,
                 conv_cfg=None,
                 norm_cfg=None,
                 activation=None):
        super(bifpn_layer, self).__init__()
        assert num_outs >= 2
        self.out_channels = out_channels
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.activation = activation
        self.num_outs = num_outs

        self.top_down_merge = nn.ModuleList()
        for i in range(self.num_outs - 1, 0, -1):
            merge_op = WeightedMerge([in_channels[i], in_channels[i-1]], out_channels, target_size_list[i-1], norm_cfg, apply_bn=True)
            self.top_down_merge.append(merge_op)

        self.bottom_up_merge = nn.ModuleList()
        for i in range(0, self.num_outs - 1):
            in_channels_list = [out_channels, in_channels[i+1], out_channels] if i < self.num_outs - 2 else [in_channels[-1], out_channels]
            merge_op = WeightedMerge(in_channels_list, out_channels, target_size_list[i+1], norm_cfg, apply_bn=True)
            self.bottom_up_merge.append(merge_op)

    def forward(self, inputs):
        assert len(inputs) == self.num_outs

        # top down merge
        md_x = []
        for i in range(self.num_outs - 1, 0, -1):
            x = self.top_down_merge[self.num_outs-i-1]([inputs[i], inputs[i-1]])
            md_x.append(x)

        # bottom up merge
        outputs = md_x[::-1]
        for i in range(1, self.num_outs - 1):
            outputs[i] = self.bottom_up_merge[i-1]([md_x[i], inputs[i], outputs[i-1]])
        outputs.append(self.bottom_up_merge[-1]([inputs[-1], outputs[-1]]))
        return outputs

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                kaiming_init(m)
            elif isinstance(m, (_BatchNorm, nn.GroupNorm)):
                constant_init(m, 1)


@NECKS.register_module
class BiFPN(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 target_size_list,
                 num_outs,
                 start_level=0,
                 end_level=-1,
                 stack=1,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN', momentum=0.003, eps=1e-4, requires_grad=True)):
        super(BiFPN, self).__init__()
        assert len(in_channels) >= 3
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.num_ins = len(in_channels)
        self.stack = stack
        self.num_outs = num_outs
        self.fp16_enabled = False

        if end_level == -1:
            self.backbone_end_level = self.num_ins
            assert num_outs >= self.num_ins - start_level
        else:
            # if end_level < inputs, no extra level is allowed
            self.backbone_end_level = end_level
            assert end_level <= len(in_channels)
            assert num_outs == end_level - start_level
        self.start_level = start_level
        self.end_level = end_level

        # add extra feature layers using resampling
        self.extra_ops = nn.ModuleList()
        for i in range(self.backbone_end_level, self.num_outs):
            in_c = in_channels[-1]
            self.extra_ops.append(
                Resample(in_c, out_channels, target_size_list[i] , norm_cfg, apply_bn=True)
            )
            in_channels.append(out_channels)

        self.stack_bifpns = nn.ModuleList()
        for _ in range(stack):
            self.stack_bifpns.append(
                bifpn_layer(in_channels,
                            out_channels,
                            target_size_list,
                            num_outs=self.num_outs,
                            conv_cfg=conv_cfg,
                            norm_cfg=norm_cfg))
            in_channels = [out_channels] * self.num_outs

    @auto_fp16()
    def forward(self, inputs):
        outs = list(inputs)
        for _, extra_op in enumerate(self.extra_ops):
            outs.append(extra_op(outs[-1]))

        for _, stack_bifpn in enumerate(self.stack_bifpns):
            outs = stack_bifpn(outs)

        return tuple(outs[:self.num_outs])

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                kaiming_init(m)
            elif isinstance(m, (_BatchNorm, nn.GroupNorm)):
                constant_init(m, 1)