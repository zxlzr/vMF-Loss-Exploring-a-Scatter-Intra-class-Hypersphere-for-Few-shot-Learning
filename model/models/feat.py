import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F

from .metircs import distanceMetric,spectralMetric
from model.models import FewShotModel

from .loss import kCosLogit

class ScaledDotProductAttention(nn.Module):
    ''' Scaled Dot-Product Attention '''

    def __init__(self, temperature, attn_dropout=0.1):
        super().__init__()
        self.temperature = temperature
        self.dropout = nn.Dropout(attn_dropout)
        self.softmax = nn.Softmax(dim=2)

    def forward(self, q, k, v):
        attn = torch.bmm(q, k.transpose(1, 2))
        attn = attn / self.temperature
        log_attn = F.log_softmax(attn, 2)
        attn = self.softmax(attn)
        attn = self.dropout(attn)
        output = torch.bmm(attn, v)
        return output, attn, log_attn


class MultiHeadAttention(nn.Module):
    ''' Multi-Head Attention module '''

    def __init__(self, n_head, d_model, d_k, d_v, dropout=0.1):
        super().__init__()
        self.n_head = n_head
        self.d_k = d_k
        self.d_v = d_v

        self.w_qs = nn.Linear(d_model, n_head * d_k, bias=False)
        self.w_ks = nn.Linear(d_model, n_head * d_k, bias=False)
        self.w_vs = nn.Linear(d_model, n_head * d_v, bias=False)
        nn.init.normal_(self.w_qs.weight, mean=0, std=np.sqrt(2.0 / (d_model + d_k)))
        nn.init.normal_(self.w_ks.weight, mean=0, std=np.sqrt(2.0 / (d_model + d_k)))
        nn.init.normal_(self.w_vs.weight, mean=0, std=np.sqrt(2.0 / (d_model + d_v)))

        self.attention = ScaledDotProductAttention(temperature=np.power(d_k, 0.5))
        self.layer_norm = nn.LayerNorm(d_model)

        self.fc = nn.Linear(n_head * d_v, d_model)
        nn.init.xavier_normal_(self.fc.weight)
        self.dropout = nn.Dropout(dropout)

    def forward(self, q, k, v):
        d_k, d_v, n_head = self.d_k, self.d_v, self.n_head
        sz_b, len_q, _ = q.size()
        sz_b, len_k, _ = k.size()
        sz_b, len_v, _ = v.size()

        residual = q
        q = self.w_qs(q).view(sz_b, len_q, n_head, d_k)
        k = self.w_ks(k).view(sz_b, len_k, n_head, d_k)
        v = self.w_vs(v).view(sz_b, len_v, n_head, d_v)

        q = q.permute(2, 0, 1, 3).contiguous().view(-1, len_q, d_k)  # (n*b) x lq x dk
        k = k.permute(2, 0, 1, 3).contiguous().view(-1, len_k, d_k)  # (n*b) x lk x dk
        v = v.permute(2, 0, 1, 3).contiguous().view(-1, len_v, d_v)  # (n*b) x lv x dv

        output, attn, log_attn = self.attention(q, k, v)

        output = output.view(n_head, sz_b, len_q, d_v)
        output = output.permute(1, 2, 0, 3).contiguous().view(sz_b, len_q, -1)  # b x lq x (n*dv)

        output = self.dropout(self.fc(output))
        output = self.layer_norm(output + residual)

        return output


class FEAT(FewShotModel):
    def __init__(self, args):
        super().__init__(args)
        if args.backbone_class == 'ConvNet':
            hdim = 64
        elif args.backbone_class == 'Res12':
            hdim = 640
        elif args.backbone_class == 'Res18':
            hdim = 512
        elif args.backbone_class == 'WRN':
            hdim = 640
        else:
            raise ValueError('')

        self.slf_attn = MultiHeadAttention(1, hdim, hdim, hdim, dropout=0.5)

        self.k_cos_loss=kCosLogit(args.kappa, args.way*args.query,args.way*args.shot)
        self.task_num=0
        self.intra_ress=[]
        self.inter_ress=[]
        self.intra_over_inter_ress=[]
        self.rho_spec_ress=[]

    def prototype_loss(self, prototypes):
        #normailisation
        prototypes = F.normalize(prototypes, dim=-1)
        # Dot product of normalized prototypes is cosine similarity.
        product = torch.matmul(prototypes, prototypes.t()) + 1
        # Remove diagnoxnal from loss.
        product -= 2. * torch.diag(torch.diag(product))
        # Minimize maximum cosine similarity.
        loss = product.max(dim=1)[0]
        return loss.mean()

    def _forward(self, instance_embs, support_idx, query_idx):
        emb_dim = instance_embs.size(-1)
        if self.training:
            self.task_num +=1

        #record->最后一轮开始统计
        if self.training and self.task_num > (self.args.max_epoch - 1) * self.args.episodes_per_epoch:
            label=[]
            for i in range(self.args.shot+self.args.query):
                for j in range(self.args.way):
                    label.append(j)
            label=torch.tensor(label,dtype=torch.int)
            feature = instance_embs.detach()
            metric = distanceMetric(mode='intra')
            intra_res = metric(feature, label)
            # print('intra_', intra_res)
            self.intra_ress.append(intra_res)
            metric = distanceMetric(mode='inter')
            inter_res = metric(feature, label)
            # print('inter_', inter_res)
            self.inter_ress.append(inter_res)
            metric = distanceMetric(mode='intra_over_inter')
            intra_over_inter_res = metric(feature, label)
            # print('intra_over_inter_', intra_over_inter_res)
            # U, s, V = torch.svd(torch.Tensor(feature))
            self.intra_over_inter_ress.append(intra_over_inter_res)
            # print('svd_',torch.sum(s))
            metric = spectralMetric(640, 1)
            rho_spec_res = metric(feature)
            # print(metric.name, ":", rho_spec_res)
            self.rho_spec_ress.append((rho_spec_res))

        # organize support/query data
        support = instance_embs[support_idx.contiguous().view(-1)].contiguous().view(*(support_idx.shape + (-1,)))
        query = instance_embs[query_idx.contiguous().view(-1)].contiguous().view(*(query_idx.shape + (-1,)))

        # get mean of the support
        proto = support.mean(dim=1)  # Ntask x NK x d
        num_batch = proto.shape[0]
        num_proto = proto.shape[1]
        num_query = np.prod(query_idx.shape[-2:])

        # query: (num_batch, num_query, num_proto, num_emb)
        # proto: (num_batch, num_proto, num_emb)
        proto = self.slf_attn(proto, proto, proto)

        #proto_loss
        if self.args.proto_ratio!=0:
            proto_loss = self.prototype_loss(proto.squeeze())
        else:
            proto_loss=0

        if self.args.use_euclidean:
            query = query.view(-1, emb_dim).unsqueeze(1)  # (Nbatch*Nq*Nw, 1, d)
            proto = proto.unsqueeze(1).expand(num_batch, num_query, num_proto, emb_dim).contiguous()
            proto = proto.view(num_batch * num_query, num_proto, emb_dim)  # (Nbatch x Nq, Nk, d)

            logits = - torch.sum((proto - query) ** 2, 2) / self.args.temperature
        elif self.args.loss_type=='cross_entropy':
            proto = F.normalize(proto, dim=-1)  # normalize for cosine distance
            query = query.view(num_batch, -1, emb_dim)  # (Nbatch,  Nq*Nw, d)

            logits = torch.bmm(query, proto.permute([0, 2, 1])) / self.args.temperature
            logits = logits.view(-1, num_proto)
        else:
            proto=proto.view( -1, emb_dim)
            query = query.view( -1, emb_dim) # (Nbatch*Nq*Nw, d)

            logits = self.k_cos_loss(query, proto)/ self.args.temperature

        # for regularization
        if self.training:
            aux_task = torch.cat([support.view(1, self.args.shot, self.args.way, emb_dim),
                                  query.view(1, self.args.query, self.args.way, emb_dim)], 1)  # T x (K+Kq) x N x d
            num_query = np.prod(aux_task.shape[1:3])
            aux_task = aux_task.permute([0, 2, 1, 3])
            aux_task = aux_task.contiguous().view(-1, self.args.shot + self.args.query, emb_dim)
            # apply the transformation over the Aug Task
            aux_emb = self.slf_attn(aux_task, aux_task, aux_task)  # T x N x (K+Kq) x d
            # compute class mean
            aux_emb = aux_emb.view(num_batch, self.args.way, self.args.shot + self.args.query, emb_dim)
            aux_center = torch.mean(aux_emb, 2)  # T x N x d

            if self.args.use_euclidean:
                aux_task = aux_task.permute([1, 0, 2]).contiguous().view(-1, emb_dim).unsqueeze(
                    1)  # (Nbatch*Nq*Nw, 1, d)
                aux_center = aux_center.unsqueeze(1).expand(num_batch, num_query, num_proto, emb_dim).contiguous()
                aux_center = aux_center.view(num_batch * num_query, num_proto, emb_dim)  # (Nbatch x Nq, Nk, d)

                logits_reg = - torch.sum((aux_center - aux_task) ** 2, 2) / self.args.temperature2
            elif self.args.loss_type=='cross_entropy':
                aux_center = F.normalize(aux_center, dim=-1)  # normalize for cosine distance
                aux_task = aux_task.permute([1, 0, 2]).contiguous().view(num_batch, -1, emb_dim)  # (Nbatch,  Nq*Nw, d)

                logits_reg = torch.bmm(aux_task, aux_center.permute([0, 2, 1])) / self.args.temperature2
                logits_reg = logits_reg.view(-1, num_proto)
            else:
                aux_center=aux_center.view(-1, emb_dim)
                aux_task = aux_task.permute([1,0,2]).contiguous().view(-1, emb_dim) # (Nbatch*Nq*Nw, d)
                logits_reg = self.k_cos_loss(aux_task, aux_center) / self.args.temperature2

        if self.training:
            return logits, proto_loss, logits_reg
        else:
            return logits, proto_loss
