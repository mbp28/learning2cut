import torch
from torch.nn import Sequential as Seq, Linear as Lin, ReLU
from torch_scatter import scatter_mean, scatter_add
from torch_geometric.nn.conv import MessagePassing, GCNConv, GATConv
import torch.nn.functional as F
from torch_sparse import spspmm
from torch_geometric.nn import TopKPooling
from torch_geometric.utils import add_self_loops, sort_edge_index, remove_self_loops, add_remaining_self_loops
from torch_geometric.utils.repeat import repeat
from collections import OrderedDict, namedtuple

from torch.nn import Parameter
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.utils import remove_self_loops, add_self_loops, softmax
from torch_geometric.nn.inits import glorot, zeros
TransformerDecoderContext = namedtuple('TransformerDecoderContext', ('edge_index', 'edge_attr'))


# Cut convolution with attention and pairwise edge attributes
class CATConv(MessagePassing):
    r"""The graph attentional operator from the `"Graph Attention Networks"
    <https://arxiv.org/abs/1710.10903>`_ paper, extended with edge attributes :math:`e_{ij}`

    .. math::
        \mathbf{x}^{\prime}_i = \alpha_{i,i}\mathbf{\Theta}\mathbf{x}_{i} +
        \sum_{j \in \mathcal{N}(i)} \alpha_{i,j}\mathbf{\Theta}\mathbf{x}_{j},

    where the attention coefficients :math:`\alpha_{i,j}` are computed as

    .. math::
        \alpha_{i,j} =
        \frac{
        \exp\left(\mathrm{LeakyReLU}\left(\mathbf{a}^{\top}
        [\mathbf{\Theta}\mathbf{x}_i \, \Vert \, \mathbf{\Theta}\mathbf{x}_j \, \Vert e_{ij}]
        \right)\right)}
        {\sum_{k \in \mathcal{N}(i) \cup \{ i \}}
        \exp\left(\mathrm{LeakyReLU}\left(\mathbf{a}^{\top}
        [\mathbf{\Theta}\mathbf{x}_i \, \Vert \, \mathbf{\Theta}\mathbf{x}_k \, \Vert e_{ij}]
        \right)\right)}.

    Args:
        in_channels (int): Size of each input sample.
        out_channels (int): Size of each output sample.
        edge_attr_dim (int): edge attributes dimensionality.
        heads (int, optional): Number of multi-head-attentions.
            (default: :obj:`1`)
        concat (bool, optional): If set to :obj:`False`, the multi-head
            attentions are averaged instead of concatenated.
            (default: :obj:`True`)
        negative_slope (float, optional): LeakyReLU angle of the negative
            slope. (default: :obj:`0.2`)
        dropout (float, optional): Dropout probability of the normalized
            attention coefficients which exposes each node to a stochastically
            sampled neighborhood during training. (default: :obj:`0`)
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    def __init__(self, in_channels, out_channels, edge_attr_dim, edge_attr_emb=4, heads=1, concat=True,
                 negative_slope=0.2, dropout=0, bias=True, **kwargs):
        super(CATConv, self).__init__(aggr='add', **kwargs)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.edge_attr_dim = edge_attr_dim
        self.edge_attr_emb = edge_attr_emb
        self.heads = heads
        self.concat = concat
        self.negative_slope = negative_slope
        self.dropout = dropout

        # projection of x_a to attention heads
        self.weight = Parameter(torch.Tensor(in_channels, heads * out_channels))
        # projection of edge_attr to attention heads
        self.edge_attr_weight = Parameter(torch.Tensor(edge_attr_dim, heads * edge_attr_emb))  # todo
        # extend the attention projection vector according to the additional edge_attr dimensions
        self.att = Parameter(torch.Tensor(1, heads, 2 * out_channels + edge_attr_emb))  # todo

        if bias and concat:
            self.bias = Parameter(torch.Tensor(heads * out_channels))
        elif bias and not concat:
            self.bias = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter('bias', None)

        self.reset_parameters()

    def reset_parameters(self):
        glorot(self.weight)
        glorot(self.att)
        zeros(self.bias)

    def forward(self, inputs):
        """
        Compute multi head attention, where the compatibility coefficient \alpha
        takes into account the edge attributes.
        For cut encoder, edge attributes e_ij can be the orthogonality of cut i and cut j.
        For cut decoder, e_ij can be the decoder information about cut i, while processing cut j.
        In the latter case, e_ij will be two bits: (processed, selected).
        :param x: torch.Tensor [|V|, d_v]
        :param edge_index: torch.Tensor [2, |E|]
        :param edge_index: torch.Tensor [|E|, d_e]
        """
        x, edge_index, edge_attr = inputs

        # masked out this original lines, we don't need them, and I don't know why they exist.
        # in addition, I don't know what is the meaning of the input size
        # originally there was input keyword size=None, and then:
        # if size is None and torch.is_tensor(x):
        #     edge_index, _ = remove_self_loops(edge_index)
        #     edge_index, _ = add_self_loops(edge_index,
        #                                    num_nodes=x.size(self.node_dim))

        if torch.is_tensor(x):
            x = torch.matmul(x, self.weight)
        else:
            x = (None if x[0] is None else torch.matmul(x[0], self.weight),
                 None if x[1] is None else torch.matmul(x[1], self.weight))
        edge_attr = torch.matmul(edge_attr, self.edge_attr_weight)
        return self.propagate(edge_index, x=x, edge_attr=edge_attr), edge_index, edge_attr  # todo verify

    def message(self, edge_index_i, x_i, x_j, size_i, edge_attr):  # todo verify
        # Compute attention coefficients.
        # split x_j and edge_attr projections to attention heads
        x_j = x_j.view(-1, self.heads, self.out_channels)
        edge_attr = edge_attr.view(-1, self.heads, self.edge_attr_emb)
        if x_i is None:  # why should it happen?
            alpha = (x_j * self.att[:, :, self.out_channels:]).sum(dim=-1)
        else:
            # split x_i projections to attention heads
            x_i = x_i.view(-1, self.heads, self.out_channels)
            # todo - split edge_attr projections to the attention heads
            # concatenate x_i to each one of its neighbors
            # including the associated edge attributes,
            # then multiply and sum to generate \alpha_ij for each attention head
            alpha = (torch.cat([x_i, x_j, edge_attr], dim=-1) * self.att).sum(dim=-1)  # todo

        alpha = F.leaky_relu(alpha, self.negative_slope)
        alpha = softmax(alpha, edge_index_i, size_i)  # todo - what is size role?

        # Sample attention coefficients stochastically.
        alpha = F.dropout(alpha, p=self.dropout, training=self.training)

        return x_j * alpha.view(-1, self.heads, 1)

    def update(self, aggr_out):
        if self.concat is True:
            aggr_out = aggr_out.view(-1, self.heads * self.out_channels)
        else:
            aggr_out = aggr_out.mean(dim=1)

        if self.bias is not None:
            aggr_out = aggr_out + self.bias
        return aggr_out

    def __repr__(self):
        return '{}({}, {}, heads={})'.format(self.__class__.__name__,
                                             self.in_channels,
                                             self.out_channels, self.heads)


# LP tripartite graph convolution
class LPConv(torch.nn.Module):
    r"""Left to right and right to left graph neural network convolution,
        inspired from the `"Exact Combinatorial Optimizationwith Graph Convolutional Neural Networks"
        <https://arxiv.org/pdf/1906.01629.pdf>`_ paper
        The right nodes are the variables,
        and the left nodes consist of `ncons` constraint nodes,
        and `ncuts` candidate cuts nodes.
        The left to right update rule is:

        .. math::
            \mathbf{v}^{\prime}_j = \mathbf{f}_v ([\mathbf{v}_j ,
            \square_{i \in \mathcal{Nc}(j)} \mathbf{g}_v (\mathbf{v}_j, \mathbf{c}_i, \mathbf{e}_{ij}),
            \square_{i \in \mathcal{Na}(j)} \mathbf{h}_v (\mathbf{v}_j, \mathbf{c}_i, \mathbf{e}_{ij})])

        where

        :math:`\square_{i \in \mathcal{Nc}(j)}` is some permutation invariant aggregation function, e.g. add or mean

        :math:`\mathcal{Nc}(j)` and :math:`\mathcal{Na}(j)` are the neighboring nodes of
          :math:`\mathbf{v}_j` among the constraints and the cuts nodes respectively.

        :math:`\mathbf{f}_v, \mathbf{g}_v` and :math:`\mathbf{h}_v` are 2-layer MLP operators with Relu activation.

        In the same manner, the right to left convolution for updating
        the cuts (cons) features is

        .. math::
            \mathbf{c}^{\prime}_i = \mathbf{f}_{cuts} ([\mathbf{c}_i ,
            \square_{j \in \mathcal{N}(i)} \mathbf{g}_{cuts} (\mathbf{c}_i, \mathbf{v}_j, \mathbf{e}_{ij})])

        where

        :math:`\square_{j \in \mathcal{N}(i)}` is some permutation invariant aggregation function, e.g. add or mean

        :math:`\mathcal{N}(i)` are the neighboring variable nodes of a cut (constraint) :math:`\mathbf{c}_i`.

        :math:`\mathbf{f}_{cuts}` and :math:`\mathbf{g}_{cuts}` are 2-layer MLP operators with Relu activation

        Since cuts and constraints do not have edges between them,
        and since cuts and constraints have different features,
        they are updated using two separated right-to-left operators.

        Updated constraint features are computed only if requested (cuts_only=False),
        because they are interesting only when cascading such CutEmbedding modules.
        In the case cuts_only=False, the returned tensor is
        corresponding to the input features tensor x, but only with the
        updated features of the constraints, variables and cuts.

        Args:
            in_channels (int): Size of each input sample.
            emb_dim (int): Size of each output sample.
            aggr (string, optional): The aggregation scheme to use
                (:obj:`"add"`, :obj:`"mean"`, :obj:`"max"`).
                (default: :obj:`"add"`)
            bias (bool, optional): If set to :obj:`False`, the layer will not learn
                an additive bias. (default: :obj:`True`)
            **kwargs (optional): Additional arguments of
                :class:`torch_geometric.nn.conv.MessagePassing`.
        """

    def __init__(self, x_v_channels, x_c_channels, x_a_channels, edge_attr_dim,
                 emb_dim=32, aggr='mean', cuts_only=True, output_relu=True):
        super(LPConv, self).__init__()
        self.x_v_channels = x_v_channels
        self.x_c_channels = x_c_channels
        self.x_a_channels = x_a_channels
        self.edge_attr_dim = edge_attr_dim
        self.emb_dim = emb_dim
        self.aggr = aggr
        self.cuts_only = cuts_only
        if aggr == 'add':
            self.aggr_func = scatter_add
        elif aggr == 'mean':
            self.aggr_func = scatter_mean

        ### LEFT TO RIGHT LAYERS ###
        # vars embedding
        self.g_v_in_channels = x_v_channels + x_c_channels + edge_attr_dim
        self.h_v_in_channels = x_v_channels + x_a_channels + edge_attr_dim
        self.f_v_in_channels = x_v_channels + emb_dim * 2
        self.g_v = Seq(Lin(self.g_v_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())
        self.h_v = Seq(Lin(self.h_v_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())
        self.f_v = Seq(Lin(self.f_v_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim))
        if output_relu:
            self.f_v = Seq(Lin(self.f_v_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())

        ### RIGHT TO LEFT LAYERS ###
        # cuts embedding
        self.g_a_in_channels = x_a_channels + emb_dim + edge_attr_dim
        self.f_a_in_channels = x_a_channels + emb_dim
        self.g_a = Seq(Lin(self.g_a_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())
        self.f_a = Seq(Lin(self.f_a_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim))
        if output_relu:
            self.f_a = Seq(Lin(self.f_a_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())

        # cons embedding:
        if not cuts_only:
            self.g_c_in_channels = x_c_channels + emb_dim + edge_attr_dim
            self.f_c_in_channels = x_c_channels + emb_dim
            self.g_c = Seq(Lin(self.g_c_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())
            self.f_c = Seq(Lin(self.f_c_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim))
            if output_relu:
                self.f_c = Seq(Lin(self.f_c_in_channels, emb_dim), ReLU(), Lin(emb_dim, emb_dim), ReLU())

    def forward(self, inputs):
        """
        Compute the left-to-right convolution of a bipartite graph.
        Assuming a PairTripartiteAndCliqueData or Batch object, d, produced by
        utils.data.get_gnn_data,
        the module inputs should be as follows:
        :param inputs: a tuple consists of
                       x_c            : d.x_c
                       x_v            : d.x_v
                       x_a            : d.x_a
                       edge_index_c2v : d.edge_index_c2v
                       edge_index_a2v : d.edge_index_a2v
                       edge_attr_c2v  : d.edge_attr_c2v
                       edge_attr_a2v  : d.edge_attr_a2v
        :return: if self.cuts_only==True: torch.Tensor([d.ncuts.sum(), out_channels])
                 else: tuple like inputs, where x_c, x_v and x_a have emb_dim features, and the rest are the same.
        """
        x_c, x_v, x_a, edge_index_c2v, edge_index_a2v, edge_attr_c2v, edge_attr_a2v = inputs
        ### LEFT TO RIGHT CONVOLUTION ###
        c2v_s, c2v_t = edge_index_c2v
        a2v_s, a2v_t = edge_index_a2v
        n_v_nodes = x_v.shape[0]
        n_a_nodes = x_a.shape[0]
        n_c_nodes = x_c.shape[0]

        # cons to vars messages:
        g_v_input = torch.cat([x_v[c2v_t],      # v_j
                               x_c[c2v_s],      # c_i
                               edge_attr_c2v],  # e_ij
                              dim=1)
        g_v_out = self.g_v(g_v_input)

        # cuts to vars messages:
        h_v_input = torch.cat([x_v[a2v_t],      # v_j
                               x_a[a2v_s],      # c_i
                               edge_attr_a2v],  # e_ij
                              dim=1)
        h_v_out = self.h_v(h_v_input)

        # aggregate messages to a tensor of size [nvars, out_channels]:
        aggr_g_v_out = self.aggr_func(g_v_out, c2v_t, dim=0, dim_size=n_v_nodes)  # TODO verify that dim_size-None is correct
        aggr_h_v_out = self.aggr_func(h_v_out, a2v_t, dim=0, dim_size=n_v_nodes)  # TODO verify that dim_size-None is correct

        # update vars features with f:
        f_v_input = torch.cat([x_v, aggr_g_v_out, aggr_h_v_out], dim=1)
        f_v_out = self.f_v(f_v_input)

        # return a tensor of size [total_nvars, out_channels]
        # this tensor should be propagated to the next layer as the updated variable nodes features
        # return f_v_out


        ### RIGHT TO LEFT CONVOLUTION ###
        # vars to cuts messages, using the updated vars features (ReLUed):
        g_a_input = torch.cat([x_a[a2v_s],             # a_i
                               f_v_out.relu()[a2v_t],  # v'_j
                               edge_attr_a2v],         # e_ij
                              dim=1)
        g_a_out = self.g_a(g_a_input)

        # aggregate messages to a tensor of size [ncuts, out_channels]:
        aggr_g_a_out = self.aggr_func(g_a_out, a2v_s, dim=0, dim_size=n_a_nodes)  # TODO verify that dim_size-None is correct

        # update cuts features with f_cuts:
        f_a_input = torch.cat([x_a, aggr_g_a_out], dim=-1)
        f_a_out = self.f_a(f_a_input)

        if not self.cuts_only:
            # do the same for the constraint nodes:
            # vars to cons messages, using the updated vars features:
            g_c_input = torch.cat([x_c[c2v_s],             # c_i
                                   f_v_out.relu()[c2v_t],  # v'_j
                                   edge_attr_c2v],         # e_ij
                                  dim=1)
            g_c_out = self.g_c(g_c_input)

            # aggregate messages to a tensor of size [ncons, out_channels]:
            aggr_g_c_out = self.aggr_func(g_c_out, c2v_s, dim=0, dim_size=n_c_nodes)  # TODO verify that dim_size-None is correct

            # update cons features with f_cons:
            f_c_input = torch.cat([x_c, aggr_g_c_out], dim=-1)
            f_c_out = self.f_c(f_c_input)

            # return the updated features of the constraint, variable and cut nodes
            return f_c_out, f_v_out, f_a_out, edge_index_c2v, edge_index_a2v, edge_attr_c2v, edge_attr_a2v

        # if embedding only cuts:
        return f_a_out


# classic convolution for cuts
class CutConv(MessagePassing):
    r"""Inter cuts convolution

    .. math::
        \mathbf{x}^{\prime}_i = \mathbf{f} \left([\mathbf{x}_i, \square_{j \in \mathcal{N}(i)}
        \mathbf{g}(\mathbf{z}_{ij})]\right)

    Where :math:`\mathbf{f}` and :math:`\mathbf{g}` denote some NN operator, e.g. mlp.

    and :math:`\mathbf{z}_{ij} = [ \mathbf{x}_i, \mathbf{x}_j, \mathbf{e}_{i,j} ]`

    The aggregation function :math:`\square` can be either 'add' or 'mean'

    Args:
        channels (int): Size of each input sample.
        edge_attr_dim (int): Edge feature dimensionality.
        aggr (string, optional): The aggregation operator to use
            (:obj:`"add"`, :obj:`"mean"`, :obj:`"max"`).
            (default: :obj:`"add"`)
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    def __init__(self, channels, edge_attr_dim, aggr='mean', **kwargs):
        super(CutConv, self).__init__(aggr=aggr, **kwargs)
        self.in_channels = channels
        self.out_channels = channels
        self.edge_attr_dim = edge_attr_dim

        self.f = Lin(2 * channels, channels)
        self.g = Lin(2 * channels + edge_attr_dim, channels)

        self.reset_parameters()

    def reset_parameters(self):
        self.f.reset_parameters()
        self.g.reset_parameters()

    def forward(self, inputs):
        """"""
        x, edge_index, edge_attr = inputs
        return self.propagate(edge_index, x=x, edge_attr=edge_attr), edge_index, edge_attr

    def message(self, x_i, x_j, edge_attr):
        z_ij = torch.cat([x_i, x_j, edge_attr], dim=-1)
        return self.g(z_ij)

    def update(self, aggr_out, x):
        x = torch.cat([x, aggr_out], dim=-1)
        return self.f(x)

    def __repr__(self):
        return '{}({}, {}, edge_attr_dim={})'.format(self.__class__.__name__,
                                                     self.in_channels, self.out_channels,
                                                     self.edge_attr_dim)


# graph self attention convolution
class SelfAttention(MessagePassing):
    r"""Graph self attention

    .. math::
        \mathbf{x}^{\prime}_i = \mathbf{f} \left([\mathbf{x}_i, \square_{j \in \mathcal{N}(i)}
        \mathbf{g}(\mathbf{z}_{ij})]\right)

    Where :math:`\mathbf{f}` and :math:`\mathbf{g}` denote some NN operator, e.g. mlp.

    and :math:`\mathbf{z}_{ij} = [ \mathbf{x}_i, \mathbf{x}_j, \mathbf{e}_{i,j} ]`

    The aggregation function :math:`\square` can be either 'add' or 'mean'

    Args:
        channels (int): Size of each input sample.
        edge_attr_dim (int): Edge feature dimensionality.
        aggr (string, optional): The aggregation operator to use
            (:obj:`"add"`, :obj:`"mean"`, :obj:`"max"`).
            (default: :obj:`"add"`)
        hidden_relu (bool, optional): Apply ReLU to :math:`\mathbf{G}` before aggregation. (default: :obj:`True`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    def __init__(self, channels, edge_attr_dim, aggr='mean', hidden_relu=True, output_relu=True, **kwargs):
        super(SelfAttention, self).__init__(aggr=aggr, **kwargs)
        self.in_channels = channels
        self.out_channels = channels
        self.edge_attr_dim = edge_attr_dim
        self.relu_g = hidden_relu
        self.relu_f = output_relu

        self.f = Lin(2 * channels, channels)
        self.g = Lin(2 * channels + edge_attr_dim, channels)

        self.reset_parameters()

    def reset_parameters(self):
        self.f.reset_parameters()
        self.g.reset_parameters()

    def forward(self, inputs):
        """"""
        x, edge_index, edge_attr = inputs
        return self.propagate(edge_index, x=x, edge_attr=edge_attr), edge_index, edge_attr

    def message(self, x_i, x_j, edge_attr):
        z_ij = torch.cat([x_i, x_j, edge_attr], dim=-1)
        return F.relu(self.g(z_ij)) if self.relu_g else self.g(z_ij)

    def update(self, aggr_out, x):
        x = torch.cat([x, aggr_out], dim=-1)
        return F.relu(self.f(x)) if self.relu_f else self.f(x)

    def __repr__(self):
        return '{}({}, {}, edge_attr_dim={})'.format(self.__class__.__name__,
                                                     self.in_channels, self.out_channels,
                                                     self.edge_attr_dim)


# transformer Q network
class TQnet(torch.nn.Module):
    def __init__(self, hparams={}, use_gpu=True, gpu_id=None):
        super(TQnet, self).__init__()
        self.hparams = hparams
        cuda_id = 'cuda' if gpu_id is None else f'cuda:{gpu_id}'
        self.device = torch.device(cuda_id if use_gpu and torch.cuda.is_available() else "cpu")
        self.select_at_least_one_cut = hparams.get('select_at_least_one_cut', True)
        assert hparams.get('tqnet_version', 'v3') == 'v3', 'v1 and v2 are deprecated'

        ###########
        # Encoder #
        ###########
        # stack lp conv layers todo consider skip connections
        self.lp_conv = Seq(OrderedDict([(f'lp_conv_{i}', LPConv(x_v_channels=hparams.get('state_x_v_channels', 13) if i==0 else hparams.get('emb_dim', 32),   # mandatory - derived from state features
                                                                x_c_channels=hparams.get('state_x_c_channels', 14) if i==0 else hparams.get('emb_dim', 32),   # mandatory - derived from state features
                                                                x_a_channels=hparams.get('state_x_a_channels', 16) if i==0 else hparams.get('emb_dim', 32),   # mandatory - derived from state features
                                                                edge_attr_dim=hparams.get('state_edge_attr_dim', 1),  # mandatory - derived from state features
                                                                emb_dim=hparams.get('emb_dim', 32),                   # default
                                                                aggr=hparams.get('lp_conv_aggr', 'mean'),             # default
                                                                cuts_only=(i == hparams.get('encoder_lp_conv_layers', 1) - 1)))
                                        for i in range(hparams.get('encoder_lp_conv_layers', 1))]))

        ###########
        # Decoder #
        ###########
        # decoder edge attributes are
        # [o_ij, o_iS, is_i_selected]
        # for all ij in the candidate cuts bidirected complete graph (including self edges)
        # where o_ij is the orthogonality between i and j,
        # o_iS is the min orthogonality between i and the selected group S
        decoder_edge_attr_dim = 3
        self.decoder_conv = {
            'SelfAttention': Seq(OrderedDict([(f'self_attention_{i}', SelfAttention(channels=hparams.get('emb_dim', 32),
                                                                                    edge_attr_dim=decoder_edge_attr_dim,
                                                                                    aggr=hparams.get('decoder_conv_aggr', 'mean')))
                                              for i in range(hparams.get('decoder_layers', 1))])),
            'CutConv': Seq(OrderedDict([(f'cut_conv_{i}', CutConv(channels=hparams.get('emb_dim', 32),
                                                                  edge_attr_dim=decoder_edge_attr_dim,
                                                                  aggr=hparams.get('decoder_conv_aggr', 'mean')))
                                        for i in range(hparams.get('decoder_layers', 1))])),
            'CATConv': Seq(OrderedDict([(f'cat_conv_{i}', CATConv(in_channels=hparams.get('emb_dim', 32),
                                                                  out_channels=hparams.get('emb_dim', 32) // hparams.get('attention_heads', 4),
                                                                  edge_attr_dim=decoder_edge_attr_dim,
                                                                  edge_attr_emb=8,
                                                                  heads=hparams.get('attention_heads', 4)))
                                        for i in range(hparams.get('decoder_layers', 1))])),
        }.get(hparams.get('decoder_conv', 'SelfAttention'))
        self.decoder_edge_attr_list = None  # moved to local context_edge_atter_list
        self.decoder_edge_index_list = None  # moved to local context_edge_index_list
        self.decoder_context = None
        self.decoder_greedy_action = None

        ##########
        # Q head #
        ##########
        self.q = Lin(hparams.get('emb_dim', 32), 2)  # Q-values for adding a cut or not

    def forward(self,
                x_c,
                x_v,
                x_a,
                edge_index_c2v,
                edge_index_a2v,
                edge_attr_c2v,
                edge_attr_a2v,
                edge_index_a2a,
                edge_attr_a2a,
                mode='inference',
                random_action=None,
                compute_J_E=False
                ):
        """
        """
        ###########
        # Encoder #
        ###########
        lp_conv_inputs = x_c, x_v, x_a, edge_index_c2v, edge_index_a2v, edge_attr_c2v, edge_attr_a2v
        cut_encoding = self.lp_conv(lp_conv_inputs)

        ###########
        # Decoder #
        ###########
        # decoding - inference
        if mode == 'inference':
            if random_action is None:
                # run greedy inference
                q_vals = self.inference(cut_encoding, edge_index_a2a, edge_attr_a2a)
            else:
                # construct decoder context according to random action, and generate the q_values in parallel.
                edge_index_a2a, edge_attr_a2a = self.get_random_context(random_action=random_action,
                                                                        initial_edge_index_a2a=edge_index_a2a,
                                                                        initial_edge_attr_a2a=edge_attr_a2a)
                decoder_inputs = (cut_encoding, edge_index_a2a, edge_attr_a2a)
                cut_decoding, _, _ = self.decoder_conv(decoder_inputs)
                q_vals = self.q(cut_decoding)
        elif mode == 'batch':
            # we are in training.
            # decode cuts in parallel given the cut-level context,
            # todo - support multi-layered decoder:
            #        1. break batch into individual graphs
            #        2. for each graph:
            #           (i)     repeat cut_encoding and edge_index_dec ncuts times,
            #                   and increment edge_index_dec for each replication by ncuts.
            #           (ii)    expand edge_index_dec such that the ith replica's edge_attr_dec
            #                   takes its values from the ith cut incoming edges.
            #           (iii)   decode q_values
            #           (iv)    q_vals[i] <- the ith cut q_values from the ith replica's
            #      - support computing J_E. (depending on the bullet above)
            decoder_inputs = (cut_encoding, edge_index_a2a, edge_attr_a2a)
            cut_decoding, _, _ = self.decoder_conv(decoder_inputs)
            # and estimate q values
            q_vals = self.q(cut_decoding)
        else:
            raise ValueError

        return q_vals

    def inference(self, cuts_encoding, edge_index_a2a, edge_attr_a2a):
        ncuts = cuts_encoding.shape[0]

        # Build the action iteratively by picking the argmax over all q_values, given a context.
        # A context is defined by edge_attr_a2a:
        # To each directed edge in edge_index_a2a we assign 3-dim vector [o_ij, oiS, is_selected(i)]
        # where o_ij is the pairwise orthogonality between cut i and cut j,
        # o_iS is the min orthogonality between cut i and the cuts in the currently selected group,
        # and is_selected(i) is indicating if i is in S or not.
        #
        # The edge index is permanent through the inference process, and only the edge attributes change.
        #
        # At the end of each iteration, before updating edge_attr_dec with the newly selected cut,
        # the edges pointing to the selected cut are stored in edge_index_list
        # together with the corresponding edge_attr_dec entries.
        # Those will serve as transformer context to train the selected cut Q value.

        # initialize the decoder with all cuts marked as (not selected)
        # old - edge_attr_dec = torch.zeros((edge_index_a2a.shape[1], ), dtype=torch.float32).to(self.device)

        context_edge_attr = edge_attr_a2a
        context_edge_index = edge_index_a2a
        # todo assert that edge_index_a2a contains all the self loops
        # edge_index_dec, edge_attr_dec = add_remaining_self_loops(edge_index_a2a, edge_weight=edge_attr_dec, fill_value=0)
        # context_edge_attr.unsqueeze_(dim=1)

        context_edge_index_list = []
        context_edge_attr_list = []

        # create a tensor of all q values to return to user
        q_vals = torch.empty(size=(ncuts, 2), dtype=torch.float32)
        selected_cuts_mask = torch.zeros(size=(ncuts,), dtype=torch.bool)

        # run loop until all cuts are selected, or the first one is discarded
        for _ in range(ncuts):
            # decode
            decoder_inputs = (cuts_encoding, context_edge_index, context_edge_attr)
            cuts_decoding, _, _ = self.decoder_conv(decoder_inputs)

            # compute q values for all cuts
            q = self.q(cuts_decoding)

            # mask already selected cuts, overriding their q_values by -inf
            q[selected_cuts_mask, :] = -float('Inf')

            # force selecting at least one cut
            # by setting the "discard" q_values of all cuts to -Inf at the first iteration only
            if self.select_at_least_one_cut and not selected_cuts_mask.any():
                cut_index = q[:, 1].argmax()
                selected = 1

                # todo - verification. remove this code after test passed
                masked_q = q.clone()
                masked_q[:, 0] = -float('Inf')
                serial_index = masked_q.argmax()
                # translate the serial index to [row, col] (or in other words [cut_index, selected])
                cut_index_old = torch.floor(serial_index.float() / 2).long()
                # a cut is selected if the maximal value is q[cut_index, 1]
                selected_old = serial_index % 2
                assert selected == selected_old and cut_index == cut_index_old

            else:
                # find argmax [cut_index, selected] and max q_value
                serial_index = q.argmax()
                # translate the serial index to [row, col] (or in other words [cut_index, selected])
                cut_index = torch.floor(serial_index.float() / 2).long()
                # a cut is selected if the maximal value is q[cut_index, 1]
                selected = serial_index % 2

            if selected:
                # append to the context list the edges pointing to the selected cut,
                # and their corresponding attr
                incoming_edges = context_edge_index[1, :] == cut_index
                incoming_edge_index = context_edge_index[:, incoming_edges]
                incoming_attr = context_edge_attr[incoming_edges, :]  # take the rows corresponding to the incoming edges
                context_edge_attr_list.append(incoming_attr.detach().cpu())
                context_edge_index_list.append(incoming_edge_index.detach().cpu())

                # update the decoder context for the next iteration
                # a. update the cut outgoing edges attributes
                outgoing_edges = context_edge_index[0, :] == cut_index
                # mark cut_index as "selected"
                context_edge_attr[outgoing_edges, -1] = float(selected)
                # update o_iS of all (remaining) cuts to min(o_iS, o_i<cut_index>)
                k = cut_index
                o_iS = context_edge_attr[:, 1]
                # broadcast the orthogonality with cut_index to all the edges
                o_ik = context_edge_attr[outgoing_edges, 0][context_edge_index[0, :]]
                # the updated o_iS is the min between the current value and the orthogonality to cut_index.
                context_edge_attr[:, 1] = torch.min(o_iS, o_ik)

                # b. store the q values of the selected cut in the output q_vals
                q_vals[cut_index, :] = q[cut_index, :]
                # c. update the selected_cuts_mask
                selected_cuts_mask[cut_index] = True
                # go to the next iteration to see if there are more useful cuts
            else:
                # stop adding cuts
                # store the current context for the remaining cuts
                remaining_cuts_mask = selected_cuts_mask.logical_not()
                remaining_cuts_idxs = remaining_cuts_mask.nonzero()
                context_edge_attr = context_edge_attr.detach().cpu()
                context_edge_index = context_edge_index.detach().cpu()
                for cut_index in remaining_cuts_idxs:
                    # append to the context list the edges pointing to the cut_index,
                    # and their corresponding attr
                    incoming_edges = context_edge_index[1, :] == cut_index
                    incoming_edge_index = context_edge_index[:, incoming_edges]
                    incoming_attr = context_edge_attr[incoming_edges, :]
                    context_edge_attr_list.append(incoming_attr)
                    context_edge_index_list.append(incoming_edge_index)
                # store the last q values of the remaining cuts in the output q_vals
                q_vals[remaining_cuts_mask, :] = q.detach().cpu()[remaining_cuts_mask, :]
                break

        if self.select_at_least_one_cut and ncuts > 0:
            assert selected_cuts_mask.any()

        # store the greedy action built on the fly to return to user,
        # since the q_values.argmax(1) is not necessarily equal to selected_cuts_mask
        self.decoder_greedy_action = selected_cuts_mask

        # finally, stack the decoder edge_attr and edge_index lists,
        # and make a "decoder context" for training the transformer
        context_edge_attr = torch.cat(context_edge_attr_list, dim=0)
        context_edge_index = torch.cat(context_edge_index_list, dim=1)
        self.decoder_context = TransformerDecoderContext(context_edge_index, context_edge_attr)
        return q_vals

    def get_random_context(self, random_action, initial_edge_index_a2a, initial_edge_attr_a2a):
        """ Construct random context according to random_action, for parallel inference. """
        ncuts = random_action.shape[0]
        # find the randomly selected cuts.
        selected_idxes = random_action.nonzero()
        # permute selection order
        selected_idxes = selected_idxes[torch.randperm(len(selected_idxes))]
        discarded_idxes = random_action.logical_not().nonzero()

        # initialize context
        context_edge_index = initial_edge_index_a2a
        context_edge_attr = initial_edge_attr_a2a

        context_edge_index_list = []
        context_edge_attr_list = []

        # process the selected cuts first
        for cut_index in selected_idxes:
            # append to the context list the edges pointing to the selected cut,
            # and their corresponding attributes
            incoming_edges = context_edge_index[1, :] == cut_index
            incoming_edge_index = context_edge_index[:, incoming_edges]
            incoming_attr = context_edge_attr[incoming_edges, :]  # take the rows corresponding to the incoming edges
            context_edge_attr_list.append(incoming_attr.detach().cpu())
            context_edge_index_list.append(incoming_edge_index.detach().cpu())

            # update the decoder context for the next iteration
            # a. update the cut outgoing edges attributes
            outgoing_edges_idxes = context_edge_index[0, :] == cut_index
            # mark cut_index as "selected"
            context_edge_attr[outgoing_edges_idxes, -1] = 1
            # update o_iS of all (remaining) cuts to min(o_iS, o_i<cut_index>)
            k = cut_index
            o_iS = context_edge_attr[:, 1]
            # broadcast the orthogonality with cut_index to all the edges
            o_ik = context_edge_attr[outgoing_edges_idxes, 0][context_edge_index[0, :]]
            # the updated o_iS is the min between the current value and the orthogonality to cut_index.
            context_edge_attr[:, 1] = torch.min(o_iS, o_ik)

        # assign the final context to all the remaining cuts
        for cut_index in discarded_idxes:
            # append to the context list the edges pointing to the cut_index,
            # and their corresponding attr
            incoming_edges = context_edge_index[1, :] == cut_index
            incoming_edge_index = context_edge_index[:, incoming_edges]
            incoming_attr = context_edge_attr[incoming_edges, :]
            context_edge_attr_list.append(incoming_attr)
            context_edge_index_list.append(incoming_edge_index)

        random_action_edge_index_a2a = torch.cat(context_edge_index_list, dim=1)
        random_action_edge_attr_a2a = torch.cat(context_edge_attr_list, dim=0)
        random_action_decoder_context = TransformerDecoderContext(random_action_edge_index_a2a, random_action_edge_attr_a2a)
        self.decoder_context = random_action_decoder_context

        return random_action_edge_index_a2a.to(self.device), random_action_edge_attr_a2a.to(self.device)


# transformer Q network - old version
class TQnetOld(torch.nn.Module):
    def __init__(self, hparams={}, use_gpu=True, gpu_id=None):
        super(TQnetOld, self).__init__()
        self.hparams = hparams
        cuda_id = 'cuda' if gpu_id is None else f'cuda:{gpu_id}'
        self.device = torch.device(cuda_id if use_gpu and torch.cuda.is_available() else "cpu")
        self.version = hparams.get('tqnet_version', 'v2')
        assert self.version in ['v1', 'v2']
        self.select_at_least_one_cut = hparams.get('select_at_least_one_cut', True)
        # select_at_least_one_cut is implemented only for 'v1' right now
        assert not (self.select_at_least_one_cut and self.version == 'v1'), 'select_at_least_one_cut is not implemented yet for v1'

        ###########
        # Encoder #
        ###########
        # stack lp conv layers todo consider skip connections
        self.lp_conv = Seq(OrderedDict([(f'lp_conv_{i}', LPConv(x_v_channels=hparams.get('state_x_v_channels', 13) if i==0 else hparams.get('emb_dim', 32),   # mandatory - derived from state features
                                                                x_c_channels=hparams.get('state_x_c_channels', 14) if i==0 else hparams.get('emb_dim', 32),   # mandatory - derived from state features
                                                                x_a_channels=hparams.get('state_x_a_channels', 16) if i==0 else hparams.get('emb_dim', 32),   # mandatory - derived from state features
                                                                edge_attr_dim=hparams.get('state_edge_attr_dim', 1),  # mandatory - derived from state features
                                                                emb_dim=hparams.get('emb_dim', 32),                   # default
                                                                aggr=hparams.get('lp_conv_aggr', 'mean'),             # default
                                                                cuts_only=(i == hparams.get('encoder_lp_conv_layers', 1) - 1)))
                                        for i in range(hparams.get('encoder_lp_conv_layers', 1))]))

        # stack cut conv layers todo consider skip connections
        self.cut_conv = {
            'CutConv': Seq(OrderedDict([(f'cut_conv_{i}', CutConv(channels=hparams.get('emb_dim', 32),
                                                                  edge_attr_dim=1,
                                                                  aggr=hparams.get('cut_conv_aggr', 'mean')))
                                        for i in range(hparams.get('encoder_cut_conv_layers', 1))])),
            'CATConv': Seq(OrderedDict([(f'cat_conv_{i}', CATConv(in_channels=hparams.get('emb_dim', 32),
                                                                  out_channels=hparams.get('emb_dim', 32) // hparams.get('attention_heads', 4),
                                                                  edge_attr_dim=1,
                                                                  edge_attr_emb=1,
                                                                  heads=hparams.get('attention_heads', 4)))
                                        for i in range(hparams.get('encoder_cut_conv_layers', 1))])),
        }.get(hparams.get('cut_conv', 'CATConv'))

        ###########
        # Decoder #
        ###########
        decoder_edge_attr_dim = 2 if self.version == 'v1' else 1
        self.decoder_conv = {
            'CutConv': Seq(OrderedDict([(f'cut_conv_{i}', CutConv(channels=hparams.get('emb_dim', 32),
                                                                  edge_attr_dim=decoder_edge_attr_dim,
                                                                  aggr=hparams.get('cut_conv_aggr', 'mean')))
                                        for i in range(hparams.get('decoder_cut_conv_layers', 1))])),
            'CATConv': Seq(OrderedDict([(f'cat_conv_{i}', CATConv(in_channels=hparams.get('emb_dim', 32),
                                                                  out_channels=hparams.get('emb_dim', 32) // hparams.get('attention_heads', 4),
                                                                  edge_attr_dim=decoder_edge_attr_dim,
                                                                  edge_attr_emb=4,
                                                                  heads=hparams.get('attention_heads', 4)))
                                        for i in range(hparams.get('decoder_cut_conv_layers', 1))])),
        }.get(hparams.get('cut_conv', 'CATConv'))
        decoder_edge_attr_list = None
        self.decoder_edge_index_list = None
        self.decoder_context = None
        self.decoder_greedy_action = None

        ##########
        # Q head #
        ##########
        self.q = Lin(hparams.get('emb_dim', 32), 2)  # Q-values for adding a cut or not

    def forward(self,
                x_c,
                x_v,
                x_a,
                edge_index_c2v,
                edge_index_a2v,
                edge_attr_c2v,
                edge_attr_a2v,
                edge_index_a2a,
                edge_attr_a2a,
                edge_attr_dec=None,
                edge_index_dec=None,
                random_action=None,
                **kwargs
                ):
        """
        :return: torch.Tensor([nvars, out_channels]) if self.cuts_only=True
                 torch.Tensor([x.shape[0], out_channels]) otherwise
        """
        # encoding
        # run lp conv and generate cut embedding
        lp_conv_inputs = x_c, x_v, x_a, edge_index_c2v, edge_index_a2v, edge_attr_c2v, edge_attr_a2v
        x_a = self.lp_conv(lp_conv_inputs)
        # run cut conv and generate cut encoding
        cut_conv_inputs = x_a, edge_index_a2a, edge_attr_a2a
        cut_encoding, _, _ = self.cut_conv(cut_conv_inputs)

        # decoding - inference
        if random_action is not None:
            # build corresponding context and run in parallel
            edge_index_dec, edge_attr_dec = self.get_random_context(random_action)

        if edge_attr_dec is None:
            if self.version == 'v1':
                q_vals = self.inference_v1(cut_encoding)
                return q_vals
            elif self.version == 'v2':
                q_vals = self.inference_v2(cut_encoding, edge_index_a2a)
                return q_vals
            else:
                raise ValueError
        else:
            # we are in training.
            # produce all q values in parallel
            # todo - support multi-layered decoder:
            #        1. break batch into individual graphs
            #        2. for each graph:
            #           (i)     repeat cut_encoding and edge_index_dec ncuts times,
            #                   and increment edge_index_dec for each replication by ncuts.
            #           (ii)    expand edge_index_dec such that the ith replica's edge_attr_dec
            #                   takes its values from the ith cut incoming edges.
            #           (iii)   decode q_values
            #           (iv)    q_vals[i] <- the ith cut q_values from the ith replica's
            decoder_inputs = (cut_encoding, edge_index_dec, edge_attr_dec)
            cut_decoding, _, _ = self.decoder_conv(decoder_inputs)
            # take the decoder output only at the cut_index and estimate q values
            return self.q(cut_decoding)

    def inference_v1(self, cut_encoding):
        ncuts = cut_encoding.shape[0]
        # rand permutation over available cuts
        inference_order = torch.randperm(ncuts)
        edge_index_dec = torch.cat([torch.arange(ncuts).view(1, -1),
                                    torch.empty((1, ncuts), dtype=torch.long)], dim=0).to(self.device)

        # initialize the decoder with all cuts marked as not (processed, selected)
        decoder_edge_index_list = []
        decoder_edge_attr_list = []
        edge_attr_dec = torch.zeros((ncuts, 2), dtype=torch.float32).to(self.device)

        # create a tensor of all q values to return to user
        q_vals = torch.empty_like(edge_attr_dec)

        # iterate over all cuts in random order, and process one cut each time
        for cut_index in inference_order:
            # set all edges to point from all cuts to the currently processed one (focus the attention mechanism)
            edge_index_dec[1, :] = cut_index

            # store the context (edge_index_dec and edge_attr_dec) of the current iteration
            decoder_edge_attr_list.append(edge_attr_dec.detach().cpu().clone())
            decoder_edge_index_list.append(edge_index_dec.detach().cpu().clone())

            # decode
            decoder_inputs = (cut_encoding, edge_index_dec, edge_attr_dec)
            cut_decoding, _, _ = self.decoder_conv(decoder_inputs)
            # take the decoder output only at the cut_index and estimate q values
            q = self.q(cut_decoding[cut_index, :])
            edge_attr_dec[cut_index, 0] = 1  # mark the current cut as processed
            edge_attr_dec[cut_index, 1] = q.argmax()  # mark the cut as selected or not, greedily according to q
            # store q in the output q_vals tensor
            q_vals[cut_index, :] = q

        # finally, stack the decoder edge_attr and edge_index tensors,
        # and make a transformer context in order to generate later a Transition for training,
        # allowing by that fast parallel backprop
        edge_attr_dec = torch.cat(decoder_edge_attr_list, dim=0)
        edge_index_dec = torch.cat(decoder_edge_index_list, dim=1)
        self.decoder_context = TransformerDecoderContext(edge_index_dec, edge_attr_dec)
        return q_vals

    def inference_v2(self, cut_encoding, edge_index_a2a):
        ncuts = cut_encoding.shape[0]

        # Build the action iteratively by picking the argmax across all q_values
        # of all cuts.
        # The edge_index_dec at each iteration is the same as edge_index_a2a,
        # and the edge_attr_dec is 1-dim vector indicating whether a cut has been already selected.
        # The not(edge_attr_dec) will serve as mask for finding the next argmax
        # At the end of each iteration, before updating edge_attr_dec with the newly selected cut,
        # the edges pointing to the selected cut are stored in edge_index_list,
        # together with the corresponding edge_attr_dec entries.
        # Those will serve as transformer context to train the selected cut Q value.

        # initialize the decoder with all cuts marked as (not selected)
        edge_attr_dec = torch.zeros((edge_index_a2a.shape[1], ), dtype=torch.float32).to(self.device)
        # todo assert that edge_index_a2a contains all the self loops
        edge_index_dec, edge_attr_dec = add_remaining_self_loops(edge_index_a2a, edge_weight=edge_attr_dec, fill_value=0)
        edge_attr_dec.unsqueeze_(dim=1)
        decoder_edge_index_list = []
        decoder_edge_attr_list = []

        # create a tensor of all q values to return to user
        q_vals = torch.empty(size=(ncuts, 2), dtype=torch.float32)
        selected_cuts_mask = torch.zeros(size=(ncuts,), dtype=torch.bool)

        # run loop until all cuts are selected, or the first one is discarded
        for _ in range(ncuts):
            # decode
            decoder_inputs = (cut_encoding, edge_index_dec, edge_attr_dec)
            cut_decoding, _, _ = self.decoder_conv(decoder_inputs)

            # compute q values for all cuts
            q = self.q(cut_decoding)

            # mask already selected cuts, overriding their q_values by -inf
            q[selected_cuts_mask, :] = -float('Inf')

            # force selecting at least one cut
            # by setting the "discard" q_values of all cuts to -Inf at the first iteration only
            if self.select_at_least_one_cut and not selected_cuts_mask.any():
                masked_q = q.clone()
                masked_q[:, 0] = -float('Inf')
                serial_index = masked_q.argmax()
            else:
                # find argmax [cut_index, selected] and max q_value
                serial_index = q.argmax()

            # translate the serial index to [row, col] (or in other words [cut_index, selected])
            cut_index = torch.floor(serial_index.float() / 2).long()
            # a cut is selected if the maximal value is q[cut_index, 1]
            selected = serial_index % 2

            if selected:
                # append to the context list the edges pointing to the selected cut,
                # and their corresponding attr
                cut_incoming_edges_mask = edge_index_dec[1, :] == cut_index
                incoming_edges = edge_index_dec[:, cut_incoming_edges_mask]
                incoming_attr = edge_attr_dec[cut_incoming_edges_mask]
                decoder_edge_attr_list.append(incoming_attr.detach().cpu())
                decoder_edge_index_list.append(incoming_edges.detach().cpu())

                # update the decoder context for the next iteration
                # a. update the cut outgoing edges attribute to "selected"
                cut_outgoing_edges_mask = edge_index_dec[0, :] == cut_index
                edge_attr_dec[cut_outgoing_edges_mask] = selected.float()
                # b. store the q values of the selected cut in the output q_vals
                q_vals[cut_index, :] = q[cut_index, :]
                # c. update the selected_cuts_mask
                selected_cuts_mask[cut_index] = True
                # go to the next iteration to see if there are more useful cuts
            else:
                # stop adding cuts
                # store the current context for the remaining cuts
                remaining_cuts_mask = selected_cuts_mask.logical_not()
                remaining_cuts_idxs = remaining_cuts_mask.nonzero()
                edge_attr_dec = edge_attr_dec.detach().cpu()
                edge_index_dec = edge_index_dec.detach().cpu()
                for cut_index in remaining_cuts_idxs:
                    # append to the context list the edges pointing to the cut_index,
                    # and their corresponding attr
                    cut_incoming_edges_mask = edge_index_dec[1, :] == cut_index
                    incoming_edges = edge_index_dec[:, cut_incoming_edges_mask]
                    incoming_attr = edge_attr_dec[cut_incoming_edges_mask]
                    decoder_edge_attr_list.append(incoming_attr)
                    decoder_edge_index_list.append(incoming_edges)
                # store the last q values of the remaining cuts in the output q_vals
                q_vals[remaining_cuts_mask, :] = q.detach().cpu()[remaining_cuts_mask, :]
                break

        if self.select_at_least_one_cut and ncuts > 0:
            assert selected_cuts_mask.any()

        # store the greedy action built on the fly to return to user,
        # since the q_values.argmax(1) is not necessarily equal to selected_cuts_mask
        self.decoder_greedy_action = selected_cuts_mask

        # finally, stack the decoder edge_attr and edge_index lists,
        # and make a "decoder context" for training the transformer
        edge_attr_dec = torch.cat(decoder_edge_attr_list, dim=0)
        edge_index_dec = torch.cat(decoder_edge_index_list, dim=1)
        self.decoder_context = TransformerDecoderContext(edge_index_dec, edge_attr_dec)
        return q_vals

    def get_random_context(self, random_action):
        ncuts = random_action.shape[0]
        if self.version == 'v1':
            inference_order = torch.randperm(ncuts)
        elif self.version == 'v2':
            selected_idxes = random_action.nonzero()
            inference_order = torch.cat([selected_idxes[torch.randperm(len(selected_idxes))],
                                         random_action.logical_not().nonzero()])

        decoder_edge_attr_list = []
        decoder_edge_index_list = []
        edge_index_dec = torch.cat([torch.arange(ncuts).view(1, -1),
                                    torch.empty((1, ncuts), dtype=torch.long)], dim=0)
        edge_attr_dec = torch.zeros((ncuts, 2), dtype=torch.float32)
        # iterate over all cuts, and assign a context to each one
        for cut_index in inference_order:
            # set all edges to point from all cuts to the currently processed one (focus the attention mechanism)
            edge_index_dec[1, :] = cut_index
            # store the context (edge_index_dec and edge_attr_dec) of the current iteration
            decoder_edge_attr_list.append(edge_attr_dec.clone())
            decoder_edge_index_list.append(edge_index_dec.clone())
            # assign the random action of cut_index to the context of the next round
            edge_attr_dec[cut_index, 0] = 1  # mark the current cut as processed
            edge_attr_dec[cut_index, 1] = random_action[cut_index]  # mark the cut as selected or not

        # finally, stack the decoder edge_attr and edge_index tensors, and make a transformer context
        random_edge_attr_dec = torch.cat(decoder_edge_attr_list, dim=0)
        if self.version == 'v2':
            # take only the "selected" attribute
            random_edge_attr_dec = random_edge_attr_dec[:, 1].unsqueeze(dim=1)

        random_edge_index_dec = torch.cat(decoder_edge_index_list, dim=1)
        random_action_decoder_context = TransformerDecoderContext(random_edge_index_dec, random_edge_attr_dec)
        self.decoder_context = random_action_decoder_context

        return random_edge_index_dec.to(self.device), random_edge_attr_dec.to(self.device)

# feed forward Q network - no recurrence
class Qnet(torch.nn.Module):
    def __init__(self, hparams={}):
        super(Qnet, self).__init__()
        self.hparams = hparams

        ###########
        # Encoder #
        ###########
        # stack lp conv layers todo consider skip connections
        self.lp_conv = Seq(OrderedDict([(f'lp_conv_{i}', LPConv(x_v_channels=hparams.get('state_x_v_channels', 13) if i == 0 else hparams.get('emb_dim', 32),
                                                                x_c_channels=hparams.get('state_x_c_channels', 14) if i == 0 else hparams.get('emb_dim', 32),
                                                                x_a_channels=hparams.get('state_x_a_channels', 16) if i == 0 else hparams.get('emb_dim', 32),
                                                                edge_attr_dim=hparams.get('state_edge_attr_dim', 1),  # mandatory - derived from state features
                                                                emb_dim=hparams.get('emb_dim', 32),  # default
                                                                aggr=hparams.get('lp_conv_aggr', 'mean'),  # default
                                                                cuts_only=(i == hparams.get('encoder_lp_conv_layers', 1) - 1)))
                                        for i in range(hparams.get('encoder_lp_conv_layers', 1))]))

        # stack cut conv layers todo consider skip connections
        self.cut_conv = {
            'CutConv': Seq(OrderedDict([(f'cut_conv_{i}', CutConv(channels=hparams.get('emb_dim', 32),
                                                                  edge_attr_dim=1,
                                                                  aggr=hparams.get('cut_conv_aggr', 'mean')))
                                        for i in range(hparams.get('encoder_cut_conv_layers', 1))])),
            'CATConv': Seq(OrderedDict([(f'cat_conv_{i}', CATConv(in_channels=hparams.get('emb_dim', 32),
                                                                  out_channels=hparams.get('emb_dim', 32) // hparams.get('attention_heads', 4),
                                                                  edge_attr_dim=1,
                                                                  edge_attr_emb=1,
                                                                  heads=hparams.get('attention_heads', 4)))
                                        for i in range(hparams.get('encoder_cut_conv_layers', 1))])),
        }.get(hparams.get('cut_conv', 'CATConv'))

        ###########
        # Decoder #
        ###########
        # todo add some standard sequential model, e.g. LSTM

        ##########
        # Q head #
        ##########
        self.q = Lin(hparams.get('emb_dim', 32), 2)  # Q-values for adding a cut or not

    def forward(self,
                x_c,
                x_v,
                x_a,
                edge_index_c2v,
                edge_index_a2v,
                edge_attr_c2v,
                edge_attr_a2v,
                edge_index_a2a,
                edge_attr_a2a,
                **kwargs
                ):
        """
        :return: torch.Tensor([nvars, out_channels]) if self.cuts_only=True
                 torch.Tensor([x.shape[0], out_channels]) otherwise
        """
        # encoding
        # run lp conv and generate cut embedding
        lp_conv_inputs = x_c, x_v, x_a, edge_index_c2v, edge_index_a2v, edge_attr_c2v, edge_attr_a2v
        x_a = self.lp_conv(lp_conv_inputs)
        # run cut conv and generate cut encoding
        cut_conv_inputs = x_a, edge_index_a2a, edge_attr_a2a
        cut_encoding, _, _ = self.cut_conv(cut_conv_inputs)

        # decoding
        # todo - add here the sequential decoder stuff.

        # compute q values
        return self.q(cut_encoding)


# imitation learning models - not relevant
class CutsSelector(torch.nn.Module):
    def __init__(self, channels, edge_attr_dim, hparams={}):
        super(CutsSelector, self).__init__()
        self.channels = channels
        self.edge_attr_dim = edge_attr_dim
        self.factorization_arch = hparams.get('factorization_arch', 'CutConv')
        self.factorization_aggr = hparams.get('factorization_aggr', 'mean')
        # TODO: support more factorizations, e.g. GCNConv, GATConv, etc.
        # In addition, support sequential selection
        self.f = {
            'CutConv': CutConv(channels, edge_attr_dim, aggr=self.factorization_aggr),
            'GraphUNet': GraphUNet(channels, channels, channels, depth=3)
        }.get(self.factorization_arch, 'CutConv')
        self.classifier = Seq(Lin(channels, 1))  # binary decision, wheter to apply the cut or not.

    def forward(self, x_a, edge_index_a2a, edge_attr_a2a, batch=None):
        """
        Assuming a PairTripartiteAndClique (or Batch) object, d,
        produced by utils.data.get_gnn_data,
        this module works on the cuts clique graph of d.
        The module applies some factorization function on the clique graph,
        and then applies a classifier to select cuts.
        The module inputs are as follows
        :param x_a: d.x_a (the updated cut features from CutsEmbedding)
        :param edge_index_a2a: d.edge_index_a2a (instance-wise cuts clique graph connectivity)
        :param edge_attr_a2a: d.edge_attr_a2a (intra-cuts orthogonality)
        :return:
        """
        # apply factorization module
        x_a = self.f(x_a, edge_index_a2a, edge_attr_a2a, batch)

        # classify
        probs = self.classifier(x_a).sigmoid()

        # classify cuts as 1 ("good") if probs > 0.5, else 0 ("bad")
        y = probs > 0.5
        return y, probs


class CutSelectionModel(torch.nn.Module):
    def __init__(self, hparams={}):
        super(CutSelectionModel, self).__init__()
        self.hparams = hparams
        assert hparams.get('cuts_embedding_layers', 1) == 1, "Not implemented"

        # cuts embedding
        self.cuts_embedding = LPConv(
            x_v_channels=hparams.get('state_x_v_channels', 13),     # mandatory - derived from state features
            x_c_channels=hparams.get('state_x_c_channels', 14),     # mandatory - derived from state features
            x_a_channels=hparams.get('state_x_a_channels', 16),     # mandatory - derived from state features
            edge_attr_dim=hparams.get('state_edge_attr_dim', 1),    # mandatory - derived from state features
            emb_dim=hparams.get('emb_dim', 32),                     # default
            aggr=hparams.get('cuts_embedding_aggr', 'mean')         # default
        )

        # cut selector
        self.cuts_selector = CutsSelector(
            channels=hparams.get('emb_dim', 32),                    # default
            edge_attr_dim=hparams.get('state_edge_attr_dim', 1),           # this is the intra cuts orthogonalities
            hparams=hparams
        )

    def forward(self, state):
        """
        :return: torch.Tensor([nvars, out_channels]) if self.cuts_only=True
                 torch.Tensor([x.shape[0], out_channels]) otherwise
        """
        cuts_embedding = self.cuts_embedding(x_c=state.x_c,
                                             x_v=state.x_v,
                                             x_a=state.x_a,
                                             edge_index_c2v=state.edge_index_c2v,
                                             edge_index_a2v=state.edge_index_a2v,
                                             edge_attr_c2v=state.edge_attr_c2v,
                                             edge_attr_a2v=state.edge_attr_a2v)

        y, probs = self.cuts_selector(x_a=cuts_embedding,
                                      edge_index_a2a=state.edge_index_a2a,
                                      edge_attr_a2a=state.edge_attr_a2a,
                                      batch=state.x_a_batch)
        return y, probs


