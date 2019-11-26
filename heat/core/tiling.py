import torch

from .communication import MPI

# from . import communication
from . import dndarray

# from . import factories
# from . import manipulations
# from . import types

__all__ = ["SquareDiagTiles"]


class SquareDiagTiles:
    def __init__(self, arr, tile_per_proc=2, lshape_map=None):
        """
        Generate the tile map and the other objects which may be useful.
        The tiles generated here are based of square tiles along the diagonal. The size of these tiles along the diagonal dictate the divisions accross
        all processes. If gshape[0] >> gshape[1] then there will be extra tiles generated below the diagonal. If gshape[0] is close to gshape[1], then
        the last tile (as well as the other tiles which correspond with said tile) will be extended to cover the whole array. However, extra tiles are
        not generated above the diagonal in the case that gshape[0] << gshape[1].

        This tiling scheme was intended for use with the QR function.

        Parameters
        ----------
        arr : DNDarray
            the array to be tiled
        tile_per_proc : int
            Default = 2
            the number of divisions per process,
            if split = 0 then this is the starting number of tile rows
            if split = 1 then this is the starting number of tile columns

        Returns
        -------
        None

        Initializes
        -----------
        __col_per_proc_list : list
            list is length of the number of processes, each element has the number of tile columns on the process whos rank equals the index
        __DNDarray = arr : DNDarray
            the whole DNDarray
        __lshape_map : torch.Tensor
            unit -> [rank, row size, column size]
            tensor filled with the shapes of the local tensors
        __tile_map : torch.Tensor
            units -> row, column, start index in each direction, process
            tensor filled with the global indices of the generated tiles
        __row_per_proc_list : list
            list is length of the number of processes, each element has the number of tile rows on the process whos rank equals the index
        __tile_columns : int
            number of tile columns
        __tile_rows : int
            number of tile rows
        """
        # lshape_map -> rank (int), lshape (tuple of the local lshape, self.lshape)
        if not isinstance(arr, dndarray.DNDarray):
            raise TypeError("self must be a DNDarray, is currently a {}".format(type(self)))

        # todo: unbalance the array if there is *only* one row/column of the diagonal on a process (send it to pr - 1)
        # todo: small bug in edge case for very small matrices with < 10 elements on a process and split = 1 with gshape[0] > gshape[1]
        if lshape_map is None:
            # create lshape map
            lshape_map = torch.zeros((arr.comm.size, len(arr.gshape)), dtype=int)
            lshape_map[arr.comm.rank, :] = torch.tensor(arr.lshape)
            arr.comm.Allreduce(MPI.IN_PLACE, lshape_map, MPI.SUM)

        # chunk map
        # is the diagonal crossed by a division between processes/where
        last_diag_pr = torch.where(lshape_map[..., arr.split].cumsum(dim=0) >= min(arr.gshape))[0][
            0
        ]
        # adjust for small blocks on the last diag pr:
        last_pr_minus1 = last_diag_pr - 1 if last_diag_pr > 0 else 0
        # print(min(arr.gshape), lshape_map[..., arr.split].cumsum(dim=0)[last_pr_minus1])
        rem_cols_last_pr = abs(
            min(arr.gshape) - lshape_map[..., arr.split].cumsum(dim=0)[last_pr_minus1]
        )  # this is the number of rows/columns after the last diagonal on the last diagonal process
        last_tile_cols = tile_per_proc
        # print(rem_cols_last_pr, last_tile_cols, tile_per_proc)
        while rem_cols_last_pr / last_tile_cols < 2:
            # todo: determine best value for this (prev at 2)
            # if there cannot be tiles formed which are at list ten items larger than 2
            #   then need to reduce the number of tiles
            last_tile_cols -= 1
            if last_tile_cols == 1:
                break

        # create lists of columns and rows for each process
        col_per_proc_list = [tile_per_proc] * (last_diag_pr.item() + 1)
        col_per_proc_list[-1] = last_tile_cols
        if last_diag_pr < arr.comm.size - 1 and arr.split == 1:
            # this is the case that the gshape[1] >> gshape[0]
            # print([tile_per_proc] * (arr.comm.size - last_diag_pr - 1).item())
            col_per_proc_list.extend([1] * (arr.comm.size - last_diag_pr - 1).item())
        row_per_proc_list = [tile_per_proc] * arr.comm.size
        # need to determine the proper number of tile rows/columns
        tile_columns = tile_per_proc * last_diag_pr + last_tile_cols
        diag_crossings = lshape_map[..., arr.split].cumsum(dim=0)[: last_diag_pr + 1]
        diag_crossings[-1] = (
            diag_crossings[-1] if diag_crossings[-1] <= min(arr.gshape) else min(arr.gshape)
        )
        diag_crossings = torch.cat((torch.tensor([0]), diag_crossings), dim=0)
        # create the tile columns sizes, saved to list
        col_inds = []
        for col in range(tile_columns.item()):
            _, lshape, _ = arr.comm.chunk(
                [diag_crossings[col // tile_per_proc + 1] - diag_crossings[col // tile_per_proc]],
                0,
                rank=int(col % tile_per_proc),
                w_size=tile_per_proc if col // tile_per_proc != last_diag_pr else last_tile_cols,
            )
            col_inds.append(lshape[0])

        total_tile_rows = tile_per_proc * arr.comm.size
        row_inds = [0] * total_tile_rows
        for c, x in enumerate(col_inds):
            # set the row indices to be the same for all of the column indices
            #   (however many there are)
            row_inds[c] = x

        if arr.gshape[0] < arr.gshape[1] and arr.split == 0:
            # need to adjust the very last tile to be the remaining
            col_inds[-1] = arr.gshape[1] - sum(col_inds[:-1])

        last_diag_pr_rows = tile_per_proc  # tile rows in the last diagonal pr
        # if last row_inds on diag process is < lshape then add rows
        #       if there is > 10 rows to add, then add a new tile row
        # if there is too little data on the last tile then combine them
        if last_diag_pr < arr.comm.size - 1 and arr.split == 0:
            # these conditions imply that arr.gshape[0] > arr.gshape[1] (assuming balanced)
            # need to find the amount of data after the diagonal
            lshape_cumsum = torch.cumsum(lshape_map[..., 0], dim=0)
            diff = lshape_cumsum[last_diag_pr] - arr.gshape[1]
            if diff > lshape_map[last_diag_pr, 0] / 2:  # todo: tune this?
                # if the shape diff is > half the data on the process
                #   then add a row after the diagonal, todo: is multiple rows faster?
                row_inds.insert(tile_columns, diff)
                row_per_proc_list[last_diag_pr] += 1
            else:
                # if the diff is < half the data on the process
                #   then extend the last row inds to be the end of the process
                row_inds[tile_columns - 1] += diff

        if arr.split == 1 and arr.gshape[1] > arr.gshape[0]:
            # if the 1st dim is > 0th dim then in split=1 the cols need to be extended
            # if len(col_inds) < sum(col_per_proc_list):
            #     col_inds.extend([0])
            col_proc_ind = torch.cumsum(torch.tensor(col_per_proc_list), dim=0)
            for pr in range(arr.comm.size):
                lshape_cumsum = torch.cumsum(lshape_map[..., 1], dim=0)
                col_cumsum = torch.cumsum(torch.tensor(col_inds), dim=0)

                diff = lshape_cumsum[pr] - col_cumsum[col_proc_ind[pr] - 1]
                # print(pr, diff, col_proc_ind, len(col_inds))
                if diff > 0 and pr <= last_diag_pr:
                    col_per_proc_list[pr] += 1
                    col_inds.insert(col_proc_ind[pr], diff)
                if pr > last_diag_pr and diff > 0:
                    col_inds.insert(col_proc_ind[pr], diff)
            # print(col_per_proc_list)

        # adjust the rows on the last process which has diagonal elements
        # this should add more rows if there if much more data, or
        #   it should merge the last rows if there is not much data remaining
        # if last_diag_pr < arr.comm.size - 1 or (
        #         last_diag_pr == arr.comm.size - 1 and row_inds[-1] == 0
        # ):
        #     # -> before this loop only the rows up to the last diagonal are set (rest are 0)
        #     # num_tiles_last_diag_pr -> *current* number of tiles on the last process
        #     num_tiles_last_diag_pr = len(col_inds) - (tile_per_proc * last_diag_pr)
        #
        #     # last_diag_pr_rows_rem -> number of rows remaining on the last diag pr (not set)
        #     last_diag_pr_rows_rem = tile_per_proc - num_tiles_last_diag_pr
        #     print(last_diag_pr_rows_rem, tile_per_proc, num_tiles_last_diag_pr)
        #     # how many tiles can be put on the last diagonal process?
        #     new_tile_rows_remaining = last_diag_pr_rows_rem // 2
        #     # above determines if there are tiles not used on the process,
        #     # need to also determine if there is a lot of data left there as well
        #
        #     # todo: determine if this should be changed to a larger number
        #     # delete entries from row_inds
        #     # need to delete tile_per_proc - (num_tiles_last_diag_pr + new_tile_rows_remaining)
        #     last_diag_pr_rows -= num_tiles_last_diag_pr + new_tile_rows_remaining
        #     print('h', last_diag_pr_rows, num_tiles_last_diag_pr, new_tile_rows_remaining)
        #     del row_inds[-1 * last_diag_pr_rows : num_tiles_last_diag_pr]
        #     row_per_proc_list[last_diag_pr] = last_diag_pr_rows if last_diag_pr_rows != 0 else num_tiles_last_diag_pr
        #
        #     if last_diag_pr_rows_rem < 2 and arr.split == 0:
        #         # if the number of rows after the diagonal is 1
        #         # then need to rechunk in the 0th dimension
        #         for i in range(last_diag_pr_rows.item()):
        #             _, lshape, _ = arr.comm.chunk(
        #                 lshape_map[last_diag_pr], 0, rank=i, w_size=last_diag_pr_rows.item()
        #             )
        #             row_inds[(tile_per_proc * last_diag_pr).item() + i] = lshape[0]

        if arr.gshape[0] > arr.gshape[1]:
            nz = torch.nonzero(torch.tensor(row_inds) == 0)
            for i in range(last_diag_pr.item() + 1, arr.comm.size):
                # loop over all of the rest of the processes
                for t in range(tile_per_proc):
                    _, lshape, _ = arr.comm.chunk(lshape_map[i], 0, rank=t, w_size=tile_per_proc)
                    # print('nz', nz, row_inds)
                    row_inds[nz[0].item()] = lshape[0]
                    nz = nz[1:]

        # combine the last tiles into one if there is too little data on the last one
        # if row_inds[-1] < 2 and arr.split == 0:  # todo: determine if this should be larger
        #     row_inds[-2] += row_inds[-1]
        #     del row_inds[-1]
        #     row_per_proc_list[-1] -= 1

        # add extra rows if there is place below the diagonal for split == 1
        if arr.gshape[0] > arr.gshape[1] and arr.split == 1:
            # need to adjust the very last tile to be the remaining
            if arr.gshape[0] - arr.gshape[1] > 10:  # todo: determine best value for this
                # use chunk and a loop over the however many tiles are desired
                num_ex_row_tiles = 1  # todo: determine best value for this
                while (arr.gshape[0] - arr.gshape[1]) // num_ex_row_tiles < 2:
                    num_ex_row_tiles -= 1
                for i in range(num_ex_row_tiles):
                    _, lshape, _ = arr.comm.chunk(
                        (arr.gshape[0] - arr.gshape[1],), 0, rank=i, w_size=num_ex_row_tiles
                    )
                    row_inds.append(lshape[0])
            else:
                # if there is no place for multiple tiles, combine the remainder with the last row
                row_inds[-1] = arr.gshape[0] - sum(row_inds[:-1])

        # need to remove blank rows for arr.gshape[0] < arr.gshape[1]
        if arr.gshape[0] < arr.gshape[1]:
            # print()
            row_inds_hold = []
            for i in torch.nonzero(torch.tensor(row_inds)).flatten():
                row_inds_hold.append(row_inds[i.item()])
            row_inds = row_inds_hold

        tile_map = torch.zeros([len(row_inds), len(col_inds), 3], dtype=torch.int)
        # if arr.split == 0:  # adjust the 1st dim to be the cumsum
        col_inds = [0] + col_inds[:-1]
        col_inds = torch.tensor(col_inds).cumsum(dim=0)
        # if arr.split == 1:  # adjust the 0th dim to be the cumsum
        row_inds = [0] + row_inds[:-1]
        row_inds = torch.tensor(row_inds).cumsum(dim=0)

        for num, c in enumerate(col_inds):  # set columns
            tile_map[:, num, 1] = c
        for num, r in enumerate(row_inds):  # set rows
            tile_map[num, :, 0] = r

        # setting of rank is different for split 0 and split 1
        if arr.split == 0:
            for p in range(last_diag_pr.item()):  # set ranks
                tile_map[tile_per_proc * p : tile_per_proc * (p + 1), :, 2] = p
            # set last diag pr rank
            tile_map[
                tile_per_proc * last_diag_pr : tile_per_proc * last_diag_pr + last_diag_pr_rows,
                :,
                2,
            ] = last_diag_pr
            # set the rest of the ranks
            st = tile_per_proc * last_diag_pr + last_diag_pr_rows
            for p in range(arr.comm.size - last_diag_pr.item() + 1):
                tile_map[st : st + tile_per_proc * (p + 1), :, 2] = p + last_diag_pr.item() + 1
                st += tile_per_proc
        elif arr.split == 1:
            st = 0
            for pr, cols in enumerate(col_per_proc_list):
                tile_map[:, st : st + cols, 2] = pr
                st += cols

        for c, i in enumerate(row_per_proc_list):
            try:
                row_per_proc_list[c] = i.item()
            except AttributeError:
                pass
        for c, i in enumerate(col_per_proc_list):
            try:
                col_per_proc_list[c] = i.item()
            except AttributeError:
                pass

        # =========================================================================================
        # : Tuple[None, Any, Any, Iterable, Tensor, Tensor, Iterable, int, int]
        self.__DNDarray = arr
        self.__col_per_proc_list = (
            col_per_proc_list if arr.split == 1 else [len(col_inds)] * len(col_per_proc_list)
        )
        self.__lshape_map = lshape_map
        self.__last_diag_pr = last_diag_pr.item()
        self.__row_per_proc_list = (
            row_per_proc_list if arr.split == 0 else [len(row_inds)] * len(row_per_proc_list)
        )
        self.__tile_map = tile_map
        self.__row_inds = list(row_inds)
        self.__col_inds = list(col_inds)
        self.__tile_columns = len(col_inds)
        self.__tile_rows = len(row_inds)

        # =========================================================================================

    @property
    def arr(self):
        """
        Returns
        -------
        DNDarray : the DNDarray for which the tiles are defined on
        """
        return self.__DNDarray

    @property
    def col_indices(self):
        """
        Returns
        -------
        list : list containing the indices of the tile columns
        """
        return self.__col_inds

    @property
    def lshape_map(self):
        """
        Returns
        -------
        torch.Tensor : map of the lshape tuples for the DNDarray given
             units -> rank (int), lshape (tuple of the local shape)
        """
        return self.__lshape_map

    @property
    def last_diagonal_process(self):
        """
        Returns
        -------
        int : the rank of the last process with diagonal elements
        """
        return self.__last_diag_pr

    @property
    def row_indices(self):
        """
        Returns
        -------
        list : list containing the indices of the tile rows
        """
        return self.__row_inds

    @property
    def tile_columns(self):
        """
        Returns
        -------
        int : number of tile columns
        """
        return self.__tile_columns

    @property
    def tile_columns_per_process(self):
        """
        Returns
        -------
        list : list containing the number of columns on all processes
        """
        return self.__col_per_proc_list

    @property
    def tile_map(self):
        """
        Returns
        -------
        torch.Tensor : map of tiles
            tile_map contains the sizes of the tiles
            units -> row, column, start index in each direction, process
        """
        return self.__tile_map

    @property
    def tile_rows(self):
        """
        Returns
        -------
        int : number of tile rows
        """
        return self.__tile_rows

    @property
    def tile_rows_per_process(self):
        """
        Returns
        -------
        list : list containing the number of rows on all processes
        """
        return self.__row_per_proc_list

    def get_start_stop(self, key):
        """
        Returns the start and stop indices which correspond to the tile/s which corresponds to the
        given key. The key MUST use global indices.

        Parameters
        ----------
        key : int, tuple, list, slice
            indices to select the tile
            STRIDES ARE NOT ALLOWED, MUST BE GLOBAL INDICES

        Returns
        -------
        tuple : (dim0 start, dim0 stop, dim1 start, dim1 stop)
        """
        split = self.__DNDarray.split
        pr = self.tile_map[key][..., 2].unique()
        if pr.numel() > 1:
            raise ValueError("Tile/s must be located on one process. currently on: {}".format(pr))
        row_inds = self.row_indices + [self.__DNDarray.gshape[0]]
        col_inds = self.col_indices + [self.__DNDarray.gshape[1]]
        row_start = row_inds[sum(self.tile_rows_per_process[:pr]) if split == 0 else 0]
        col_start = col_inds[sum(self.tile_columns_per_process[:pr]) if split == 1 else 0]

        if not isinstance(key, (tuple, list, slice, int)):
            raise TypeError(
                "key must be an int, tuple, or slice, is currently {}".format(type(key))
            )

        if isinstance(key, (slice, int)):
            key = (key, slice(0, None))

        # only need to do this in 2 dimensions (this class is only for 2D right now)
        if not isinstance(key[0], (int, slice, torch.Tensor)):
            raise TypeError(
                "Key elements must be ints, slices, or torch.Tensors; currently {}".format(
                    type(key[0])
                )
            )
        if not isinstance(key[1], (int, slice, torch.Tensor)):
            raise TypeError(
                "Key elements must be ints, slices, or torch.Tensors; currently {}".format(
                    type(key[1])
                )
            )

        key = list(key)
        if isinstance(key[0], int):
            st0 = row_inds[key[0]] - row_start
            sp0 = row_inds[key[0] + 1] - row_start
        if isinstance(key[0], slice):
            start = row_inds[key[0].start] if key[0].start is not None else 0
            stop = row_inds[key[0].stop] if key[0].stop is not None else row_inds[-1]
            st0, sp0 = start - row_start, stop - row_start
        if isinstance(key[1], int):
            st1 = col_inds[key[1]] - col_start
            sp1 = col_inds[key[1] + 1] - col_start
        if isinstance(key[1], slice):
            start = col_inds[key[1].start] if key[1].start is not None else 0
            stop = col_inds[key[1].stop] if key[1].stop is not None else col_inds[-1]
            st1, sp1 = start - col_start, stop - col_start

        return st0, sp0, st1, sp1

    def get_tile_proc(self, key):
        """
        Get the process rank for the tile/s corresponding to the given key

        Parameters
        ----------
        key : int, slice, tuple
            collection of indices which correspond to a tile

        Returns
        -------
        single element torch.tensor : process rank
        """
        return self.tile_map[key][..., 2].unique()

    def get_tile_size(self, key):
        """
        Get the size of the tile/s specified by the key

        Parameters
        ----------
        key : int, slice, tuple
            collection of indices which correspond to a tile

        Returns
        -------
        tuple : dimension 0 size, dimension 1 size
        """
        tup = self.get_start_stop(key)
        return tup[1] - tup[0], tup[3] - tup[2]

    def __getitem__(self, key):
        """
        Standard getitem function for the tiles. The returned item is a view of the original
        DNDarray, operations which are done to this view will change the original array.
        **STRIDES ARE NOT AVAILABLE, NOR ARE CROSS-SPLIT SLICES**

        Parameters
        ----------
        key : int, slice, tuple, list
            indices of the tile/s desired

        Returns
        -------
        DNDarray_view : torch.Tensor
            A local selection of the DNDarray corresponding to the tile/s desired
        """
        arr = self.__DNDarray
        tile_map = self.__tile_map
        local_arr = arr._DNDarray__array
        if tile_map[key][..., 2].unique().nelement() > 1:
            # print(tile_map)
            raise ValueError("Slicing across splits is not allowed")
        # print(tile_map)
        # early outs (returns nothing if the tile does not exist on the process)
        if tile_map[key][..., 2].unique().nelement() == 0:
            return None
        if arr.comm.rank != tile_map[key][..., 2].unique():
            return None

        if not isinstance(key, (int, tuple, slice)):
            raise TypeError(
                "key must be an int, tuple, or slice, is currently {}".format(type(key))
            )

        split = self.__DNDarray.split
        rank = arr.comm.rank
        row_inds = self.row_indices + [self.__DNDarray.gshape[0]]
        col_inds = self.col_indices + [self.__DNDarray.gshape[1]]
        row_start = row_inds[sum(self.tile_rows_per_process[:rank]) if split == 0 else 0]
        col_start = col_inds[sum(self.tile_columns_per_process[:rank]) if split == 1 else 0]

        if isinstance(key, int):
            key = [key]
        else:
            key = list(key)

        if len(key) == 1:
            key.append(slice(0, None))

        # only need to do this in 2 dimensions (this class is only for 2D right now)
        if not isinstance(key[0], (int, slice)):
            raise TypeError(
                "Key elements must be ints, or slices; currently {}".format(type(key[0]))
            )
        if not isinstance(key[1], (int, slice)):
            raise TypeError(
                "Key elements must be ints, or slices; currently {}".format(type(key[1]))
            )

        key = list(key)
        if isinstance(key[0], int):
            st0 = row_inds[key[0]] - row_start
            sp0 = row_inds[key[0] + 1] - row_start
        if isinstance(key[0], slice):
            start = row_inds[key[0].start] if key[0].start is not None else 0
            stop = row_inds[key[0].stop] if key[0].stop is not None else row_inds[-1]
            st0, sp0 = start - row_start, stop - row_start
        if isinstance(key[1], int):
            st1 = col_inds[key[1]] - col_start
            sp1 = col_inds[key[1] + 1] - col_start
        if isinstance(key[1], slice):
            start = col_inds[key[1].start] if key[1].start is not None else 0
            stop = col_inds[key[1].stop] if key[1].stop is not None else col_inds[-1]
            st1, sp1 = start - col_start, stop - col_start
        return local_arr[st0:sp0, st1:sp1]

    def local_get(self, key):
        """
        Getitem routing using local indices, converts to global indices then uses getitem

        Parameters
        ----------
        key : int, slice, tuple, list
            indices of the tile/s desired
            if the stop index of a slice is larger than the end will be adjusted to the maximum
            allowed

        Returns
        -------
        torch.Tensor : the local tile/s corresponding to the key given
        """
        rank = self.__DNDarray.comm.rank
        key = self.local_to_global(key=key, rank=rank)
        return self.__getitem__(key)

    def local_set(self, key, data):
        """
        Setitem routing to set data to a local tile (using local indices)

        Parameters
        ----------
        key : int, slice, tuple, list
            indices of the tile/s desired
            if the stop index of a slice is larger than the end will be adjusted to the maximum
            allowed
        data : torch.Tensor, int, float
            data to be written to the tile

        Returns
        -------
        None
        """
        rank = self.__DNDarray.comm.rank
        key = self.local_to_global(key=key, rank=rank)
        self.__getitem__(tuple(key)).__setitem__(slice(0, None), data)

    def local_to_global(self, key, rank):
        """
        Convert local indices to global indices

        Parameters
        ----------
        key : int, slice, tuple, list
            indices of the tile/s desired
            if the stop index of a slice is larger than the end will be adjusted to the maximum
            allowed
        rank : process rank

        Returns
        -------
        tuple : key with global indices
        """
        arr = self.__DNDarray
        if isinstance(key, (int, slice)):
            key = [key, slice(0, None)]
        else:
            key = list(key)

        if arr.split == 0:
            # need to adjust key[0] to be only on the local tensor
            prev_rows = sum(self.__row_per_proc_list[:rank])
            loc_rows = self.__row_per_proc_list[rank]
            # print(self.tile_map)
            if isinstance(key[0], int):
                key[0] += prev_rows
            elif isinstance(key[0], slice):
                start = key[0].start + prev_rows if key[0].start is not None else prev_rows
                stop = key[0].stop + prev_rows if key[0].stop is not None else prev_rows + loc_rows
                stop = stop if stop - start < loc_rows else start + loc_rows
                key[0] = slice(start, stop)

        if arr.split == 1:
            loc_cols = self.__col_per_proc_list[rank]
            prev_cols = sum(self.__col_per_proc_list[:rank])
            # need to adjust key[0] to be only on the local tensor
            # need the number of columns *before* the process
            if isinstance(key[1], int):
                key[1] += prev_cols
            elif isinstance(key[1], slice):
                start = key[1].start + prev_cols if key[1].start is not None else prev_cols
                stop = key[1].stop + prev_cols if key[1].stop is not None else prev_cols + loc_cols
                stop = stop if stop - start < loc_cols else start + loc_cols
                key[1] = slice(start, stop)
        return tuple(key)

    def match_tiles(self, tiles_to_match):
        """
        function to match the tile sizes of another tile map
        NOTE: this is intended for use with the Q matrix, to match the tiling of a/R
        For this to work properly it is required that the 0th dim of both matrices is equal

        Parameters
        ----------
        tiles_to_match : SquareDiagTiles
            the tiles which should be matched by the current tiling scheme

        Returns
        -------
        None

        Notes
        -----
        This function overwrites most, if not all, of the elements of the tiling class
        """
        if not isinstance(tiles_to_match, SquareDiagTiles):
            raise TypeError(
                "tiles_to_match must be a SquareDiagTiles object, currently: {}".format(
                    type(tiles_to_match)
                )
            )
        base_dnd = self.__DNDarray
        match_dnd = tiles_to_match.__DNDarray
        # this map will take the same tile row and column sizes up to the last diagonal row/column
        # the last row/column is determined by the number of rows/columns on the non-split dimension
        # last_col_row = (
        #     tiles_to_match.tile_rows_per_process[-1]
        #     if base_dnd.split == 1
        #     else tiles_to_match.tile_columns_per_process[-1]
        # )
        # working with only split=0 for now todo: split=1
        if base_dnd.split == match_dnd.split == 0:
            # this implies that the gshape[0]'s are equal
            # rows are the exact same, and the cols are also equal to the rows (square matrix)
            self.__row_per_proc_list = tiles_to_match.__row_per_proc_list.copy()
            self.__row_inds = tiles_to_match.__row_inds.copy()
            self.__tile_rows = tiles_to_match.__tile_rows
            self.__col_per_proc_list = tiles_to_match.__row_per_proc_list.copy()
            self.__col_inds = tiles_to_match.__row_inds.copy()
            self.__tile_columns = tiles_to_match.__tile_rows
            # print(self.__row_inds)
            # pass
        # if base_dnd.split == tiles_to_match.__DNDarray.split == 0:
        #     # **the number of tiles on rows and columns will be equal for this new tile map**
        #     if base_dnd.split == 0:
        #         new_rows = tiles_to_match.tile_rows_per_process.copy()
        #     else:
        #         # this is assuming that the array is split
        #         new_rows = tiles_to_match.tile_columns_per_process.copy()
        #
        #     # set the columns which are less than the last col_row to be the same as the last one
        #     # for i in range(last_col_row):
        #     # if split=0 then can just set the columns easily
        #     new_row_inds = []
        #     new_col_inds = []
        #     new_row_inds.extend(tiles_to_match.row_indices)
        #     new_col_inds.extend(tiles_to_match.col_indices)
        #
        #     match_shape = tiles_to_match.__DNDarray.shape
        #     match_end_dim = 0 if match_shape[0] <= match_shape[1] else 1
        #     match_diag_end = tiles_to_match.__DNDarray.shape[match_end_dim]
        #     self_diag_end = sum(
        #         self.lshape_map[: tiles_to_match.last_diagonal_process + 1][..., base_dnd.split]
        #     )
        #     # below only needs to run if there is enough space for another block (>=2 entries)
        #     # and only if the last diag pr is not the last one
        #     if (
        #         tiles_to_match.last_diagonal_process != base_dnd.comm.size - 1
        #         and base_dnd.split == 0
        #     ):
        #         if self_diag_end - match_diag_end >= 2:
        #             new_row_inds.insert(last_col_row, match_diag_end)
        #             new_rows[tiles_to_match.last_diagonal_process] += 1
        #         if self_diag_end - match_end_dim < 2:
        #             new_row_inds[last_col_row] = match_diag_end
        #         new_col_inds = new_row_inds.copy()
        #
        #     if (
        #         tiles_to_match.last_diagonal_process != base_dnd.comm.size - 1
        #         and base_dnd.split == 1
        #         and self_diag_end - match_diag_end >= 2
        #     ):
        #         new_col_inds.insert(last_col_row, match_diag_end)
        #         new_rows[tiles_to_match.last_diagonal_process] += 1
        #         new_row_inds = new_col_inds.copy()
        #
        #     # # create the new tile_map of all zeros
        #     # # units -> row, column, start index in each direction, process
        #     new_tile_map = torch.zeros([sum(new_rows), sum(new_rows), 3], dtype=torch.int)
        #
        #     new_tile_map[0][..., 1] = 1
        #     proc_list = torch.cumsum(torch.tensor(new_rows), dim=0)
        #     pr, pr_hold = 0, 0
        #     for c in range(sum(new_rows)):
        #         new_tile_map[..., 1][c] = torch.tensor(new_col_inds)
        #         new_tile_map[c][..., 0] = new_col_inds[c]
        #         if pr_hold == proc_list[0]:
        #             pr += 1
        #             proc_list = proc_list[1:]
        #         pr_hold += 1
        #         new_tile_map[c][..., 2] = pr
        #     self.__tile_map = new_tile_map
        #     # other things to set:
        #     self.__col_per_proc_list = new_rows
        #     self.__row_per_proc_list = new_rows
        #     self.__last_diag_pr = tiles_to_match.last_diagonal_process
        #     self.__row_inds = new_row_inds
        #     self.__col_inds = new_col_inds
        #     self.__tile_columns = len(new_col_inds)
        #     self.__tile_rows = len(new_row_inds)

        if (
            base_dnd.split != tiles_to_match.__DNDarray.split
            and base_dnd.shape == tiles_to_match.__DNDarray.shape
        ):
            # if the shapes are the same (also both are square)
            # then only need to change the processes indices
            # flipping rows and columns for an array that is the same size with a different split
            self.__col_per_proc_list = tiles_to_match.__row_per_proc_list
            self.__row_per_proc_list = tiles_to_match.__col_per_proc_list
            self.__row_inds = tiles_to_match.__col_inds
            self.__col_inds = tiles_to_match.__row_inds
            self.__last_diag_pr = tiles_to_match.last_diagonal_process  # shapes are equal
            # need to create the new tile map
            new_tile_map = tiles_to_match.tile_map.clone()
            tile_per_proc = self.__col_per_proc_list[0]
            last_diag_pr_rows = self.__row_per_proc_list[self.__last_diag_pr]
            for p in range(self.__last_diag_pr):  # set ranks
                new_tile_map[:, tile_per_proc * p : tile_per_proc * (p + 1), 2] = p
            # set last diag pr rank
            new_tile_map[
                :,
                tile_per_proc * self.__last_diag_pr : tile_per_proc * self.__last_diag_pr
                + last_diag_pr_rows,
                2,
            ] = self.__last_diag_pr
            # set the rest of the ranks
            st = tile_per_proc * self.__last_diag_pr + last_diag_pr_rows
            for p in range(base_dnd.comm.size - self.__last_diag_pr + 1):
                new_tile_map[:, st : st + tile_per_proc * (p + 1), 2] = p + self.__last_diag_pr + 1
                st += tile_per_proc
            self.__tile_map = new_tile_map
            # other things to set:
            self.__tile_columns = tiles_to_match.__tile_rows
            self.__tile_rows = tiles_to_match.__tile_columns

        if base_dnd.split == 0 and match_dnd.split == 1:
            # rows determine the q sizes -> cols = rows
            self.__col_inds = (
                tiles_to_match.__row_inds.copy()
                if base_dnd.gshape[0] <= base_dnd.gshape[1]
                else tiles_to_match.__col_inds.copy()
            )
            self.__row_inds = (
                tiles_to_match.__row_inds.copy()
                if base_dnd.gshape[0] <= base_dnd.gshape[1]
                else tiles_to_match.__col_inds.copy()
            )

            rows_per = [x for x in self.__col_inds if x < base_dnd.shape[0]]
            self.__tile_rows = len(rows_per)
            self.__tile_columns = self.tile_rows

            target_0 = tiles_to_match.lshape_map[..., 1][: tiles_to_match.last_diagonal_process]
            end_tag0 = base_dnd.shape[0] - sum(target_0[: tiles_to_match.last_diagonal_process])
            end_tag0 = [end_tag0] + [0] * (
                base_dnd.comm.size - 1 - tiles_to_match.last_diagonal_process
            )
            target_0 = torch.cat((target_0, torch.tensor(end_tag0)), dim=0)

            targe_map = self.lshape_map.clone()
            targe_map[..., 0] = target_0
            target_0_c = torch.cumsum(target_0, dim=0)
            self.__row_per_proc_list = []
            st = 0
            rows_per = torch.tensor(rows_per + [base_dnd.shape[0]])
            for i in range(base_dnd.comm.size):
                # get the amount of data on each process, get the number of rows with
                # indices which are between the start and stop
                self.__row_per_proc_list.append(
                    torch.where((st < rows_per) & (rows_per <= target_0_c[i]))[0].numel()
                )
                st = target_0_c[i]

            base_dnd.redistribute_(lshape_map=self.lshape_map, target_map=targe_map)

            self.__tile_map = torch.zeros((self.tile_rows, self.tile_columns, 3), dtype=torch.int)
            for i in range(self.tile_rows):
                self.__tile_map[..., 0][i] = self.__row_inds[i]
            for i in range(self.tile_columns):
                self.__tile_map[..., 1][:, i] = self.__col_inds[i]
            for i in range(self.arr.comm.size):
                scale = self.__row_per_proc_list[i]
                self.__tile_map[..., 2][i * scale : (i + 1) * scale] = i
            # to adjust if the last process has more tiles
            i = self.arr.comm.size - 1

            self.__tile_map[..., 2][sum(self.__row_per_proc_list[:i]) :] = i
            self.__col_per_proc_list = [self.tile_columns] * base_dnd.comm.size
            self.__last_diag_pr = base_dnd.comm.size - 1

    def overwrite_arr(self, arr):
        """

        Parameters
        ----------
        arr

        Returns
        -------

        """
        self.__DNDarray = arr

    def __setitem__(self, key, value):
        """
        Item setter,
        uses the torch item setter and the getitem routines to set the values of the original array
        (arr in __init__)

        Parameters
        ----------
        key : int, slice, tuple, list
            tile indices to identify the target tiles
        value : int, torch.Tensor, etc.
            values to be set

        Returns
        -------
        None
        """
        arr = self.__DNDarray
        tile_map = self.__tile_map
        if arr.comm.rank == tile_map[key][..., 2].unique():
            # this will set the tile values using the torch setitem function
            arr = self.__getitem__(key)
            arr.__setitem__(slice(0, None), value)
