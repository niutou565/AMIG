# Copyright 2022 The Ip2Region Authors. All rights reserved.
# Use of this source code is governed by a Apache2.0-style
# license that can be found in the LICENSE file.
#
# xdb 查询客户端（内置，支持 IPv4 + IPv6）
# 移植自官方 py-ip2region 3.0.4（https://github.com/lionsoul2014/ip2region）
# 许可证：Apache 2.0
import io
import ipaddress

# xdb 全局常量
HeaderInfoLength = 256
VectorIndexRows = 256
VectorIndexCols = 256
VectorIndexSize = 8
VectorIndexLength = VectorIndexRows * VectorIndexCols * VectorIndexSize  # 524288

XdbStructure20 = 2
XdbStructure30 = 3
XdbIPv4Id = 4
XdbIPv6Id = 6


def le_get_uint32(buff: bytes, offset: int) -> int:
    """从 buff 的 offset 处解码一个无符号 32 位整数（小端序）"""
    return (
        (buff[offset] & 0x000000FF)
        | ((buff[offset + 1] << 8) & 0x0000FF00)
        | ((buff[offset + 2] << 16) & 0x00FF0000)
        | ((buff[offset + 3] << 24) & 0xFF000000)
    )


def le_get_uint16(buff: bytes, offset: int) -> int:
    """从 buff 的 offset 处解码一个无符号 16 位整数（小端序）"""
    return (buff[offset] & 0x000000FF) | ((buff[offset + 1] << 8) & 0x0000FF00)


class Header(object):
    """xdb 文件头（256 字节）"""

    def __init__(self, buff: bytes):
        self.version = le_get_uint16(buff, 0)
        self.indexPolicy = le_get_uint16(buff, 2)
        self.createdAt = le_get_uint32(buff, 4)
        self.startIndexPtr = le_get_uint32(buff, 8)
        self.endIndexPtr = le_get_uint32(buff, 12)
        # IPv6 支持后新增字段
        self.ipVersion = le_get_uint16(buff, 16)
        self.runtimePtrBytes = le_get_uint16(buff, 18)
        self.buff = buff


def parse_ip(ip_string: str) -> bytes:
    """把字符串 IP 解析为 packed bytes（IPv4=4字节，IPv6=16字节）"""
    try:
        return ipaddress.ip_address(ip_string).packed
    except ValueError:
        raise ValueError("invalid ip address `{}`".format(ip_string))


def ip_sub_compare(ip1: bytes, buff: bytes, offset: int) -> int:
    """IPv6 比较：xdb 索引里存的是大端序，直接按字节比较"""
    ip2 = buff[offset:offset + len(ip1)]
    if ip1 > ip2:
        return 1
    elif ip1 < ip2:
        return -1
    return 0


def _v4_sub_compare(ip1: bytes, buff: bytes, offset: int) -> int:
    """IPv4 比较：ip1 为大端序（来自 ipaddress），索引里为小端序，需倒序比较"""
    j = offset + len(ip1) - 1
    for i in range(len(ip1)):
        i1 = ip1[i]
        i2 = buff[j]
        if i1 < i2:
            return -1
        if i1 > i2:
            return 1
        j -= 1
    return 0


class Version(object):
    """IP 版本描述：字节数 + 段索引大小 + 比较函数"""

    def __init__(self, id, name, byte_num, index_size, compare_fn):
        self.id = id
        self.name = name
        self.byte_num = byte_num
        self.index_size = index_size
        self._compare_fn = compare_fn

    def ip_sub_compare(self, ip1: bytes, buff: bytes, offset: int) -> int:
        return self._compare_fn(ip1, buff, offset)


# IPv4: 4 字节 IP，段索引 14 = 4(起始) + 4(结束) + 2(数据长度) + 4(数据指针)
IPv4 = Version(XdbIPv4Id, "IPv4", 4, 14, _v4_sub_compare)
# IPv6: 16 字节 IP，段索引 38 = 16(起始) + 16(结束) + 2(数据长度) + 4(数据指针)
IPv6 = Version(XdbIPv6Id, "IPv6", 16, 38, ip_sub_compare)


def version_from_header(header: Header):
    """根据文件头判断该 xdb 是 IPv4 还是 IPv6"""
    # 旧版 xdb 2.0 仅支持 IPv4
    if header.version < XdbStructure30:
        return IPv4
    # xdb 3.0 及以上版本
    if header.ipVersion == XdbIPv4Id:
        return IPv4
    elif header.ipVersion == XdbIPv6Id:
        return IPv6
    return None


class XdbSearcher(object):
    """xdb 查询器，同时支持 IPv4 和 IPv6"""

    def __init__(self, version, dbfile=None, vector_index=None, c_buffer=None):
        self.version = version
        self._dbfile = dbfile
        self._handle = None
        self.vector_index = None
        self.c_buffer = None
        if c_buffer is not None:
            self.c_buffer = c_buffer
        else:
            self._handle = io.open(dbfile, "rb")
            self.vector_index = vector_index

    def search(self, ip) -> str:
        """查询 IP 归属地，返回 `国家|省份|城市|ISP|...` 字符串，查不到返回空串"""
        if isinstance(ip, str):
            ip_bytes = parse_ip(ip)
        elif isinstance(ip, bytes):
            ip_bytes = ip
        else:
            raise ValueError("invalid ip address `{}`".format(ip))

        # IP 版本不匹配（例如拿 IPv4 地址查 IPv6 库），直接返回空
        if len(ip_bytes) != self.version.byte_num:
            return ""

        # 通过 vector index 定位段索引块
        i0 = ip_bytes[0]
        i1 = ip_bytes[1]
        idx = i0 * VectorIndexCols * VectorIndexSize + i1 * VectorIndexSize
        if self.vector_index is not None:
            s_ptr = le_get_uint32(self.vector_index, idx)
            e_ptr = le_get_uint32(self.vector_index, idx + 4)
        elif self.c_buffer is not None:
            offset = HeaderInfoLength + idx
            s_ptr = le_get_uint32(self.c_buffer, offset)
            e_ptr = le_get_uint32(self.c_buffer, offset + 4)
        else:
            buff = self.read(HeaderInfoLength + idx, VectorIndexSize)
            s_ptr = le_get_uint32(buff, 0)
            e_ptr = le_get_uint32(buff, 4)

        # 空指针说明数据缺失
        if s_ptr == 0 or e_ptr == 0:
            return ""

        # 二分查找段索引块
        _bytes = len(ip_bytes)
        _d_bytes = _bytes << 1
        index_size = self.version.index_size
        d_len = 0
        d_ptr = 0
        l = 0
        h = int((e_ptr - s_ptr) / index_size)
        while l <= h:
            m = (l + h) >> 1
            p = int(s_ptr + m * index_size)
            buff = self.read(p, index_size)
            if self.version.ip_sub_compare(ip_bytes, buff, 0) < 0:
                h = m - 1
            elif self.version.ip_sub_compare(ip_bytes, buff, _bytes) > 0:
                l = m + 1
            else:
                d_len = le_get_uint16(buff, _d_bytes)
                d_ptr = le_get_uint32(buff, _d_bytes + 2)
                break

        if d_len == 0:
            return ""
        return self.read(d_ptr, d_len).decode("utf-8")

    def read(self, offset: int, length: int) -> bytes:
        if self.c_buffer is not None:
            return self.c_buffer[offset:offset + length]
        self._handle.seek(offset)
        return self._handle.read(length)

    def close(self):
        if self._handle is not None:
            self._handle.close()


def load_header_from_file(db_file: str) -> Header:
    handle = io.open(db_file, "rb")
    header = Header(handle.read(HeaderInfoLength))
    handle.close()
    return header


def load_vector_index_from_file(db_file: str) -> bytes:
    handle = io.open(db_file, "rb")
    handle.seek(HeaderInfoLength)
    v_index = handle.read(VectorIndexLength)
    handle.close()
    return v_index


def loadVectorIndexFromFile(db_file: str) -> bytes:
    """向后兼容别名"""
    return load_vector_index_from_file(db_file)
