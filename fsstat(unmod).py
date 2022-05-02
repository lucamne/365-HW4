"""Get information about a FAT32 filesystem and each file."""
import json
import os
import sys
from typing import Optional

import hw4utils


def unpack(data: bytes, signed=False, byteorder="little") -> int:
    """Unpack a single value from bytes"""
    return int.from_bytes(data, byteorder=byteorder, signed=signed)


class Fat:
    def __init__(self, filename):
        """Parses a FAT32 filesystem"""
        self.filename = filename
        self.file = open(self.filename, "rb")
        # set of key/value pairs parsed from the "Reserved"
        # sector of the filesystem
        self.boot = dict()
        self._parse_reserved_sector()

    def __del__(self):
        """Called when the object is destroyed."""
        # close the open file reader
        self.file.close()

    def _parse_reserved_sector(self):
        """Parse information from the "Reserved" sector of the filesystem.

        The start of the FAT32 must be at the start of self.file.

        Stores the following keys in the self.boot dictionary:
            bytes_per_sector
            sectors_per_cluster
            reserved_sectors
            number_of_fats
            total_sectors
            sectors_per_fat
            root_dir_first_cluster
            bytes_per_cluster
            fat0_sector_start
            fat0_sector_end
            data_start
            data_end

        This function also stores fat0 in self.fat.

        Refer to Carrier Chapters 9 and 10.
        """
        boot_sector = self.file.read(512)

        bytes_per_sector = unpack(boot_sector[11:13])
        sectors_per_cluster = unpack(boot_sector[13:14])
        reserved_sectors = unpack(boot_sector[14:16])
        number_of_fats = unpack(boot_sector[16:17])

        total_sectors = unpack(boot_sector[19:21])
        if total_sectors == 0:
            total_sectors = unpack(boot_sector[32:36])

        sectors_per_fat = unpack(boot_sector[36:40])
        root_dir_first_cluster = unpack(boot_sector[44:48])
        bytes_per_cluster = bytes_per_sector * sectors_per_cluster
        fat0_sector_start = reserved_sectors
        fat0_sector_end = fat0_sector_start + sectors_per_fat - 1
        data_start = fat0_sector_start + sectors_per_fat * number_of_fats
        data_end = total_sectors - 1

        self.boot = {
            "bytes_per_sector": bytes_per_sector,
            "sectors_per_cluster": sectors_per_cluster,
            "reserved_sectors": reserved_sectors,
            "number_of_fats": number_of_fats,
            "total_sectors": total_sectors,
            "sectors_per_fat": sectors_per_fat,
            "root_dir_first_cluster": root_dir_first_cluster,
            "bytes_per_cluster": bytes_per_cluster,
            "fat0_sector_start": fat0_sector_start,
            "fat0_sector_end": fat0_sector_end,
            "data_start": data_start,
            "data_end": data_end,
        }

        fat0_sector_size = self.file.seek(fat0_sector_start * bytes_per_sector)
        fat0 = self.file.read(fat0_sector_size * bytes_per_sector)
        self.fat = fat0

    def info(self):
        """Print already-parsed information about the FAT filesystem as a json string"""

        # Print out all keys stored in the self.boot dictionary
        print(json.dumps(self.boot, indent=4))

        # Parsing the root directory
        all_files = self.parse_dir(self.boot["root_dir_first_cluster"])
        for file in all_files:
            print(json.dumps(file))

    def _to_sector(self, cluster: int) -> int:
        """Given a cluster, returns the corresponding sector in data."""
        return (cluster - 2) * self.boot["sectors_per_cluster"] + self.boot[
            "data_start"
        ]

    def _end_sector(self, cluster: int) -> int:
        """Given a cluster, returns the last sector of the cluster."""
        return self._to_sector(cluster) + self.boot["sectors_per_cluster"] - 1

    def _get_sectors(self, number: int) -> list[int]:
        """Return list of sectors for a given table entry number

        This function follws the cluster chains in the file allocation table.
        Accordingly, the sectors may be non-contiguous. If the first table
        entry is 0, then an empty list is returned.
        When the end-of-file marker is found, the chain ends.
        It's important to not follow the chains recursively, because you'll
        quickly hit Python's recursion limit.

        returns:
            list[int]: list of sectors
        """

        sectors = []
        current_cluster = number
        fat_entry = self._get_fat_entry(current_cluster)

        while fat_entry != 0:
            starting_sector = self._to_sector(current_cluster)
            # Starting with the starting_sector, consecutive sectors are
            # appended to the sector list based on how many sectors
            # are in a cluster
            for i in range(self.boot["sectors_per_cluster"]):
                sectors.append(starting_sector + i)

            if fat_entry > 0x0FFFFFF8:
                break

            current_cluster = fat_entry
            fat_entry = self._get_fat_entry(current_cluster)

        assert (
            0 < (number * 4 + 4) < self.boot["sectors_per_fat"]
        ), f"{number} exceeds FAT size"

        return sectors

    def _get_fat_entry(self, cluster: int) -> int:
        """Given a cluster, returns the value of the corresponding entry in fat."""
        # Each entry in fat is 4 bytes
        return unpack(self.fat[cluster * 4 : (cluster * 4) + 4])

    def _retrieve_data(self, cluster: int, ignore_unallocated=False) -> bytes:
        """Read in the data for a given file allocation table entry number
        (i.e., the cluster number).

        Important: this function returns all bytes in the cluster, even the slack data past the
        actual filesize.

        Because the cluster chain may be non-contiguous,
        the sectors may be non-contiguous and we read in sectors one at a time.
        The results are returned as a contiguous byte string.

        If ignore_unallocated is False, then when the cluster is unallocated,
        we return an empty bytes() object.

        When you are read, start to deal with the case when ignore_unallocated is True. In that case,
        then instead of returning an empty bytes object, we read in the sectors associated
        with the cluster. For example, assume cluster 2 starts at sector 1000, and there are
        2 sectors per cluster. Then for cluster 4, if discover it is unallocated, we would
        return 1000+ (4-2)*2 = 1004 as well as 1005 (since
        the cluster consists of 2 sectors). We are likely reading data for the wrong file.

        returns:
            bytes: data (possibly zero length)
        """

        fat_entry = self._get_fat_entry(cluster)
        if ignore_unallocated and fat_entry == 0:
            sectors = [self._to_sector(cluster)]
        else:
            sectors = self._get_sectors(cluster)

        byte_sequence = b""

        bytes_per_sector = self.boot["bytes_per_sector"]

        for sector in sectors:
            self.file.seek(sector * bytes_per_sector)
            byte_sequence += self.file.read(bytes_per_sector)

        return byte_sequence

    def _get_first_cluster(self, entry: bytes) -> int:
        """Returns the first cluster of the content of a given directory entry

        This function parses a directory entry to determine the first cluster used to
        store the data. That is, it returns the FAT entry number assoicated with the directory
        entry. Based on Carrier's Table 10.5. This is a little tricky with the shifting etc,
        and so I'm providing the code.

        Expects that self.boot["total_sectors"] and self.boot["sectors_per_cluster"] exist.

        returns:
            int: cluster number
        """
        high_order = int.from_bytes(entry[20:22], "little") << 16
        low_order = int.from_bytes(entry[26:28], "little")
        content_cluster = high_order + low_order
        max_cluster = self.boot["total_sectors"] / self.boot["sectors_per_cluster"]
        # if you send the wrong data to this function, you'll hit this error
        assert content_cluster <= max_cluster, "Error: value exceeds cluster count."

        return content_cluster

    def _get_content(self, cluster: int, filesize: int) -> tuple[str, Optional[str]]:
        """Return initial content of a directory entry and the intial content of its slack data if possible.

        Read the data for a file that begins with the stated cluster. Return the first 128 bytes
        of the file (or up to the filesize, which ever is smaller).

        If the cluster is not unallocated, the slack return is from the last sector of the
        last chain of the cluster chain. Up to 32 bytes of slack is returned.

        If cluster is unallocated, then return the file content (up to 128
        bytes even though it may be the wrong file) and return None for the slack


        returns:
            str: file content (up to 128 bytes)
            str (or None if unallocated cluster): slack content (up to 32 bytes)

        """
        cluster_data = self._retrieve_data(cluster, True)
        content = str(cluster_data[0 : min(128, filesize)])
        if self._get_fat_entry(cluster) == 0:
            slack = None
        else:
            slack = str(cluster_data[filesize : min(filesize + 32, len(cluster_data))])

        return (content, slack)

    def parse_dir(self, cluster: int, parent="") -> list[dict]:
        """Parse a directory cluster, returns a list of dictionaries, one dict per entry.

        This function recursively parses any entry that is itself a directory.

        Each dictionary contains the following keys (7 keys total):
            - parent: parent directory
            - dir_cluster: cluster number of the directory
            - entry_num: entry number of the directory (within its cluster)
            - dir_sectors: sectors associated with the directory (converted from cluster)
            - entry_type: type of entry (vol, lfn, dir, or other)
            - name: name of the entry
            - deleted: whether the entry is marked as deleted

        You can use hw4utils.get_entry_type() to get the type
        YOu can use hw4utils.parse_name() to get the name

        If the entry is a directory, then the following keys are
        also present (8 keys total):
            - content_cluster: the first cluster that contain's this entry's content

        If the entry is not a vol, lfn, or dir, then the following keys are
        also present (12 keys total):
            - filesize: size of the entry
            - content_sectors: the list of sectors associated with the content of this entry
            - content: the first 128 bytes of the entry's content
            - slack: the slack data (up to 32 bytes)

        returns:
            list[dict]: list of dictionaries, one dict per entry
        """
        dict_list = []
        count = 0
        starting_byte = 0
        dir_data = self._retrieve_data(cluster)

        while len(dir_data) - starting_byte >= 32:

            directory_attribute = unpack(
                dir_data[starting_byte + 11 : starting_byte + 12]
            )
            content_cluster_bytes = (
                dir_data[starting_byte + 26 : starting_byte + 28]
                + dir_data[starting_byte + 20 : starting_byte + 22]
            )
            content_cluster = unpack(content_cluster_bytes)
            entry_type = hw4utils.get_entry_type(directory_attribute)

            allocation_byte = unpack(dir_data[starting_byte : starting_byte + 1])
            is_deleted = False
            if allocation_byte == 0 or allocation_byte == 0xE5:
                is_deleted = True

            entry_dict = {
                "parent": parent,
                "dir_cluster": cluster,
                "entry_num": count,
                "dir_sectors": self._get_sectors(cluster),
                "entry_type": entry_type,
                "name": hw4utils.parse_name(
                    dir_data[starting_byte : starting_byte + 32]
                ),
                "deleted": is_deleted,
            }

            if entry_type == "dir" and count >= 2:
                entry_dict["content_cluster"] = content_cluster
                dict_list += self.parse_dir(
                    content_cluster, parent + "/" + entry_dict["name"]
                )

            if entry_type not in ["vol", "lfn", "dir"]:
                filesize = unpack(dir_data[starting_byte + 28 : starting_byte + 32])
                entry_dict["filesize"] = filesize
                entry_dict["content_cluster"] = content_cluster
                entry_dict["content_sectors"] = self._get_sectors(content_cluster)
                entry_dict["content"], entry_dict["slack"] = self._get_content(
                    content_cluster, filesize
                )
            dict_list.append(entry_dict)

            count += 1
            starting_byte += 32

        return dict_list


def main():
    # Parse command line arguments
    if len(sys.argv) != 2:
        print(f"usage:\n\t {os.path.basename(sys.argv[0])} filename")
        exit()
    filename = sys.argv[1]
    # Parse the file and print results
    fs = Fat(filename)
    fs.info()


if __name__ == "__main__":
    main()
