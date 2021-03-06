import torch
from torch_geometric.data import Data
import logging
import numpy as np
from abc import ABC, abstractmethod
import time
from time import time as now
from funlib.segment.arrays import replace_values

from gnn_agglomeration import utils

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class HemibrainGraph(Data, ABC):

    # can't overwrite __init__ using different args than base class, but use kwargs

    @abstractmethod
    def read_and_process(
            self,
            graph_provider,
            embeddings,
            block_offset,
            block_shape,
            inner_block_offset,
            inner_block_shape):
        """
        Initiates reading the graph from DB and converting it to the desired format for torch_geometric.
        Assigns values to all torch_geometric.Data attributes

        Args:
            graph_provider (daisy.persistence.MongoDbGraphProvider):

                connection to RAG DB

            block_offset (``list`` of ``int``):

                block offset of extracted graph, in nanometers

            block_shape (``list`` of ``int``):

                block shape of extracted graph, in nanometers

            inner_block_offset (``list`` of ``int``):

                offset of sub-block, which might be used for masking, in nanometers

            inner_block_shape (``list`` of ``int``):

                shape of sub-block, which might be used for masking, in nanometers
        """

        pass

    def assert_graph(self):
        """
        check whether bi-directed edges are next to each other in edge_index
        """
        start = now()
        uv = self.edge_index[:, 0::2]
        vu = torch.flip(self.edge_index, dims=[0])[:, 1::2]

        assert torch.equal(uv, vu)
        # remove config property so Data object can be saved with torch
        del self.config
        logger.debug(f'assert graph in {now() - start} s')

    def parse_rag_excerpt(self, nodes_list, edges_list, embeddings, all_nodes):

        # TODO parametrize the used names
        id_field = 'id'
        node1_field = 'u'
        node2_field = 'v'
        merge_score_field = 'merge_score'
        gt_merge_score_field = self.config.gt_merge_score_field
        merge_labeled_field = self.config.merge_labeled_field

        node_attrs = utils.to_np_arrays(nodes_list)
        edges_attrs = utils.to_np_arrays(edges_list)

        start = now()
        u_in = np.isin(edges_attrs[node1_field], node_attrs[id_field])
        # sanity check: all u nodes should be contained in the nodes extracted by mongodb
        assert np.sum(~u_in) == 0

        v_in = np.isin(edges_attrs[node2_field], node_attrs[id_field], invert=True)
        missing_node_ids = np.unique(edges_attrs[node2_field][v_in])
        list_id_field = []
        list_center_z = []
        list_center_y = []
        list_center_x = []
        for i in missing_node_ids:
            logger.debug(f'append node {i} to node_attrs')
            list_id_field.append(i)
            list_center_z.append(all_nodes[i]['center_z'])
            list_center_y.append(all_nodes[i]['center_y'])
            list_center_x.append(all_nodes[i]['center_x'])
        node_attrs[id_field] = np.concatenate(
            (node_attrs[id_field], np.array(list_id_field))
        )
        node_attrs['center_z'] = np.concatenate(
            (node_attrs['center_z'], np.array(list_center_z))
        )
        node_attrs['center_y'] = np.concatenate(
            (node_attrs['center_y'], np.array(list_center_y))
        )
        node_attrs['center_x'] = np.concatenate(
            (node_attrs['center_x'], np.array(list_center_x))
        )

        logger.debug(f'add missing nodes to node_attrs in {now() - start} s')
        logger.info(
            f'parse graph with {len(node_attrs[id_field])} nodes, {len(edges_attrs[node1_field])} edges')

        if embeddings is None:
            x = torch.ones(len(node_attrs[id_field]), 1, dtype=torch.float)
        else:
            # TODO this is for debugging. Later, I should have an embedding for each node
            # embeddings_list = [embeddings[i] if i in embeddings else np.random.rand(10) for i in
            #                    node_attrs[id_field]]
            start = now()
            embeddings_list = [embeddings[i] for i in node_attrs[id_field]]
            x = torch.tensor(embeddings_list, dtype=torch.float)
            logger.debug(f'load embeddings from dict in {now() - start} s')

        node_ids_np = node_attrs[id_field].astype(np.int64)
        node_ids = torch.tensor(node_ids_np, dtype=torch.long)

        # By not operating inplace and providing out_array, we always use
        # the C++ implementation of replace_values

        logger.debug(
            f'before: interval {node_ids_np.max() - node_ids_np.min()}, min id {node_ids_np.min()}, max id {node_ids_np.max()}, shape {node_ids_np.shape}')
        start = time.time()
        edges_node1 = np.zeros_like(
            edges_attrs[node1_field], dtype=np.int64)
        edges_node1 = replace_values(
            in_array=edges_attrs[node1_field].astype(np.int64),
            old_values=node_ids_np,
            new_values=np.arange(len(node_attrs[id_field]), dtype=np.int64),
            inplace=False,
            out_array=edges_node1
        )
        edges_attrs[node1_field] = edges_node1
        logger.debug(
            f'remapping {len(edges_attrs[node1_field])} edges (u) in {time.time() - start} s')
        logger.debug(
            f'edges after: min id {edges_attrs[node1_field].min()}, max id {edges_attrs[node1_field].max()}')

        start = time.time()
        edges_node2 = np.zeros_like(
            edges_attrs[node2_field], dtype=np.int64)
        edges_node2 = replace_values(
            in_array=edges_attrs[node2_field].astype(np.int64),
            old_values=node_ids_np,
            new_values=np.arange(len(node_attrs[id_field]), dtype=np.int64),
            inplace=False,
            out_array=edges_node2)
        edges_attrs[node2_field] = edges_node2
        logger.debug(
            f'remapping {len(edges_attrs[node2_field])} edges (v) in {time.time() - start} s')
        logger.debug(
            f'edges after: min id {edges_attrs[node2_field].min()}, max id {edges_attrs[node2_field].max()}')

        # TODO I could potentially avoid transposing twice
        # edge index requires dimensionality of (2,e)
        # pyg works with directed edges, duplicate each edge here
        edge_index_undir = np.array(
            [edges_attrs[node1_field], edges_attrs[node2_field]])
        edge_attr_undir = edges_attrs[merge_score_field]

        # add edges, together with a dummy merge score. extend mask, edgewise label
        if self.config.self_loops:
            start_self_loops = now()
            num_nodes = len(node_attrs[id_field])
            loops = np.stack(
                [np.arange(num_nodes, dtype=np.int64), np.arange(num_nodes, dtype=np.int64)])
            edge_index_undir = np.concatenate(
                [edge_index_undir, loops], axis=1)

            edge_attr_undir = np.concatenate(
                [edge_attr_undir, np.zeros(num_nodes)], axis=0)

            edges_attrs[merge_labeled_field] = np.concatenate(
                [edges_attrs[merge_labeled_field],
                 np.zeros(num_nodes)]
            )

            edges_attrs[gt_merge_score_field] = np.concatenate(
                [edges_attrs[gt_merge_score_field],
                 np.zeros(num_nodes)]
            )
            logger.debug(f'add self loops in {now() - start} s')

        edge_index_undir = edge_index_undir.transpose()
        edge_index_dir = np.repeat(edge_index_undir, 2, axis=0)
        edge_index_dir[1::2, :] = np.flip(edge_index_dir[1::2, :], axis=1)
        edge_index = torch.tensor(edge_index_dir.astype(
            np.int64).transpose(), dtype=torch.long)

        edge_attr_undir = np.expand_dims(
            edge_attr_undir, axis=1)
        edge_attr_dir = np.repeat(edge_attr_undir, 2, axis=0)
        edge_attr = torch.tensor(edge_attr_dir, dtype=torch.float)

        pos = torch.transpose(
            input=torch.tensor(
                [
                    node_attrs['center_z'],
                    node_attrs['center_y'],
                    node_attrs['center_x']],
                dtype=torch.float),
            dim0=0,
            dim1=1)

        # Targets operate on undirected edges, therefore no duplicate necessary
        mask = torch.tensor(
            edges_attrs[merge_labeled_field],
            dtype=torch.float)
        y = torch.tensor(
            edges_attrs[gt_merge_score_field],
            dtype=torch.long)

        return edge_index, edge_attr, x, pos, node_ids, mask, y

    def class_balance_mask(self, y, mask):
        start = now()
        y = y.numpy()
        mask = mask.numpy()
        used_y = y[mask.astype(np.bool)]
        # numpy unique labels are sorted
        labels, counts = np.unique(used_y, return_counts=True)
        logger.debug(f'labels {labels}, counts {counts}')

        # TODO this is still dirty, not general purpose
        if len(labels) == 1:
            logger.debug('only one class in batch, no reweighting performed')
            return torch.tensor(mask, dtype=torch.float)
        if labels.min() != 0 or labels.max() != len(labels) - 1:
            raise NotImplementedError(
                'weight balancing only for contiguous labels starting at 0')

        weights = 1.0 / ((counts.astype(np.float32) /
                          counts.sum()) * len(labels))
        weights = weights.astype(np.float32)
        weighted_y = weights[y.astype(np.int_)]
        weighted_mask = mask * weighted_y

        logger.debug(f'perform class balancing on mask in {now() - start}')
        return torch.tensor(weighted_mask, dtype=torch.float)

    # TODO update this
    def plot_predictions(self, config, pred, graph_nr, run, acc, logger):
        pass
