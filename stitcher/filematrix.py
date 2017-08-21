"""Parse input file names, compute tile coordinates."""

import os
import re
import logging

import json
import yaml

import pandas as pd
import networkx as nx

from .inputfile import InputFile


logger = logging.getLogger('FileMatrix')


def parse_file_name(file_name):
    """Parse fields (stage coordinates) contained in `file_name`.

    Parameters
    ----------
    file_name : str
                The string to be parsed

    Returns
    -------
    x, y, z : int
        The parsed stage coordinates.
    """
    file_name = os.path.basename(file_name)
    m = re.search('^.*x_([-]?\d+).*y_([-]?\d+).*z_([-]?\d+).*', file_name)
    if m is None:
        m = re.search('^([-]?\d+)_([-]?\d+)_([-]?\d+)', file_name)
    if m is None:
        raise ValueError('Invalid name {}'.format(file_name))

    fields = []
    for i in range(1, 4):
        fields.append(int(m.group(i)))

    print('{} \tX={} Y={} Z={}'.format(file_name, *fields))
    return fields


class FileMatrix:
    """Data structures for a matrix of input files."""
    def __init__(self, directory=None, ascending_tiles_x=True,
                 ascending_tiles_y=True):
        self.dir = directory

        self.data_frame = None
        """A :class:`pandas.DataFrame` object. Contains the following
        columns: `X`, `Y`, `Z`, `Z_end`, `xsize`, `ysize`, `nfrms`,
        `filename`."""

        self.ascending_tiles_x = ascending_tiles_x
        self.ascending_tiles_y = ascending_tiles_y

        if directory is None:
            return
        if os.path.isdir(directory):
            self.load_dir(directory)
        elif os.path.isfile(directory):
            self.load_yaml(directory)

    def load_dir(self, dir=None):
        """Look for files in `dir` recursively and populate data structures.

        Parameters
        ----------
        dir : path
        """
        if dir is None:
            dir = self.dir

        if dir is None:
            return

        flist = []

        for root, dirs, files in os.walk(dir):
            if os.path.basename(root):
                try:
                    self.parse_and_append(root, flist)
                    continue
                except (RuntimeError, ValueError):
                    pass

            for f in files:
                try:
                    self.parse_and_append(os.path.join(root, f), flist)
                    continue
                except (RuntimeError, ValueError):
                    pass

        data = {'X': flist[0::7], 'Y': flist[1::7], 'Z': flist[2::7],
                'nfrms': flist[3::7], 'ysize': flist[4::7],
                'xsize': flist[5::7], 'filename': flist[6::7]}
        df = pd.DataFrame(data)
        df = df.sort_values(['Z', 'Y', 'X'])

        if not self.ascending_tiles_x:
            df['X'] = (df['X'] - df['X'].max()).abs()

        if not self.ascending_tiles_y:
            df['Y'] = (df['Y'] - df['Y'].max()).abs()

        self.data_frame = df.set_index('filename')
        self.process_data_frame()

    def load_yaml(self, fname):
        with open(fname, 'r') as f:
            y = yaml.load(f)

        attrs = ['ascending_tiles_x', 'ascending_tiles_y']

        for attr in attrs:
            setattr(self, attr, y['xcorr-options'][attr])

        self.data_frame = pd.DataFrame(y['filematrix']).set_index('filename')

        self.process_data_frame()
        self.dir = fname

    def process_data_frame(self):
        df = self.data_frame

        xsize = df['X'].unique().size
        ysize = df['Y'].unique().size
        n_of_files = len(df.index)

        if xsize * ysize != n_of_files:
            msg = 'Mosaic is {}x{} tiles, but there are {} files!'.format(
                xsize, ysize, n_of_files)
            raise ValueError(msg)

        keys = ['X', 'Y', 'Z']
        df[keys] -= df[keys].min()

        df['Z_end'] = df['Z'] + df['nfrms']

        cols = df.columns
        if 'Xs' in cols and 'Ys' in cols and 'Zs' in cols:
            for key in ['Xs', 'Ys', 'Zs']:
                df[key] -= df[key].min()
            df['Xs_end'] = df['Xs'] + df['xsize']
            df['Ys_end'] = df['Ys'] + df['ysize']
            df['Zs_end'] = df['Zs'] + df['nfrms']

    def parse_and_append(self, name, flist):
        try:
            fields = parse_file_name(name)
            with InputFile(os.path.join(self.dir, name)) as infile:
                fields.append(infile.nfrms)
                fields.append(infile.ysize)
                fields.append(infile.xsize)
            flist += fields
            flist.append(name)
        except (RuntimeError, ValueError):
            raise

    def save_to_yaml(self, filename, mode):
        keys = ['X', 'Y', 'Z', 'nfrms', 'xsize', 'ysize']
        abs_keys = ['Xs', 'Ys', 'Zs']
        for k in abs_keys:
            if k in self.data_frame.columns:
                keys.append(k)
        df = self.data_frame[keys].reset_index()
        j = json.loads(df.to_json(orient='records'))

        if mode == 'update':
            with open(filename, 'r') as f:
                y = yaml.load(f)

            y['filematrix'] = j

            with open(filename, 'w') as f:
                yaml.dump(y, f, default_flow_style=False)
        else:
            with open(filename, mode) as f:
                yaml.dump({'filematrix': j}, f, default_flow_style=False)

    @property
    def slices(self):
        """A slice is a group of tiles that share at least a `z` frame.

        Returns
        -------
        comp : generator
            A generator of graphs, one for each connected component of G,
            where G is the graph of tiles connected by at least a `z` frame.
        """
        G = nx.Graph()
        for index, row in self.data_frame.iterrows():
            G.add_node(index)

        for index, row in self.data_frame.iterrows():
            view = self.data_frame[
                (self.data_frame['Z'] <= row['Z'])
                & (self.data_frame['Z_end'] >= row['Z_end'])
                ]
            pairs = zip(view.index.values[::1], view.index.values[1::1])
            G.add_edges_from(pairs)
            G.add_edge((view.index.values[0]), view.index.values[-1])

        return nx.connected_component_subgraphs(G)

    @property
    def tiles_along_dir(self):
        """Groups of tiles to be stitched along a given direction.

        You need to send to this generator a tuple containing:
            - a list for sorting the :class:`pandas.DataFrame`, such as \
            :code:`['Z', 'Y', 'X']`

            - an axis for grouping, such as :code:`'Y'`

        Yields
        -------
        :class:`pandas.DataFrame`
            A group of tiles
        """
        for s in self.slices:
            got = yield
            view = self.data_frame.loc[s.nodes()].sort_values(
                got[0], ascending=True).groupby(got[1])
            for name, group in view:
                yield group

    @property
    def tiles_along_X(self):
        """Groups of tiles to be stitched along `X`.

        Equivalent to :attr:`~tiles_along_dir` having sent the following
        tuple: :code:`(['Z', 'X', 'Y'], 'Y')`

        Yields
        -------
        :class:`pandas.DataFrame`
            A group of tiles
        """
        g = self.tiles_along_dir
        next(g)
        yield g.send((['Z', 'X', 'Y'], 'Y'))
        yield from g

    @property
    def tiles_along_Y(self):
        """Groups of tiles to be stitched along `Y`.

        Equivalent to :attr:`~tiles_along_dir` having sent the following
        tuple: :code:`(['Z', 'Y', 'X'], 'X')`

        Yields
        -------
        :class:`pandas.DataFrame`
            A group of tiles
        """
        g = self.tiles_along_dir
        next(g)
        yield g.send((['Z', 'Y', 'X'], 'X'))
        yield from g

    @property
    def full_width(self):
        return self.data_frame['Xs_end'].max()

    @property
    def full_height(self):
        return self.data_frame['Ys_end'].max()

    @property
    def full_thickness(self):
        return self.data_frame['Zs_end'].max()
