import torch
import logging
import daisy

from .hemibrain_graph import HemibrainGraph
from gnn_agglomeration.utils import TooManyEdgesException

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class HemibrainGraphUnmasked(HemibrainGraph):

    # can't overwrite __init__ using different args than base class

    def read_and_process(
            self,
            graph_provider,
            embeddings,
            all_nodes,
            block_offset,
            block_shape,
            inner_block_offset,
            inner_block_shape):

        assert self.config is not None

        logger.debug(
            'read\n'
            f'\tblock offset: {block_offset}\n'
            f'\tblock shape: {block_shape}'
        )

        roi = daisy.Roi(list(block_offset), list(block_shape))
        node_attrs = graph_provider.read_nodes(roi=roi)
        edge_attrs = graph_provider.read_edges(roi=roi, nodes=node_attrs)

        if len(node_attrs) == 0:
            raise ValueError('No nodes found in roi %s' % roi)
        if len(edge_attrs) == 0:
            raise ValueError('No edges found in roi %s' % roi)

        # TODO this is untested
        # can be used for masking of nodes later on
        self.inner_roi_offset = torch.tensor(inner_block_offset, dtype=torch.long)
        self.inner_roi_shape = torch.tensor(inner_block_shape, dtype=torch.long)

        # PyG doubles all edges, there * 2 here
        if len(edge_attrs) * 2 > self.config.max_edges:
            raise TooManyEdgesException(
                f'extracted graph has {len(edge_attrs) * 2} edges, but the limit is set to {self.config.max_edges}')

        self.edge_index, \
            self.edge_attr, \
            self.x, \
            self.pos, \
            self.node_ids, \
            self.mask, \
            self.y = self.parse_rag_excerpt(
                node_attrs, edge_attrs, embeddings, all_nodes)

        if self.edge_index.size(1) > self.config.max_edges:
            raise ValueError(
                f'extracted graph has {self.edge_index.size(1)} edges, but the limit is set to {self.config.max_edges}')

        self.mask = self.class_balance_mask(y=self.y, mask=self.mask)
        self.roi_mask = torch.ones_like(self.mask, dtype=torch.uint8)

        self.assert_graph()
