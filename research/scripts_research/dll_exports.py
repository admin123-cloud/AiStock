import struct

data = open(r'E:\AStockHistory\wstock\解压工具\wsWDZ\wdz_app\wszEX.dll', 'rb').read()
pe = struct.unpack_from('<I', data, 0x3c)[0]
nsec = struct.unpack_from('<H', data, pe + 6)[0]
opt = struct.unpack_from('<H', data, pe + 20)[0]
export_rva = struct.unpack_from('<II', data, pe + 24 + 96)[0]
secoff = pe + 24 + opt
secs = []
for i in range(nsec):
    vsize, vaddr, rsize, raddr = struct.unpack_from('<IIII', data, secoff + i * 40 + 8)
    secs.append((vaddr, vaddr + vsize, raddr))

def rva2off(rva):
    for va0, va1, ra in secs:
        if va0 <= rva < va1:
            return ra + (rva - va0)
    return None

off = rva2off(export_rva)
n_names, = struct.unpack_from('<I', data, off + 24) if False else (None,)
# standard fields: Characteristics(4) TimeDate(4) Major(2) Minor(2) Name(4) Base(4) NumberOfFunctions(4) NumberOfNames(4)
nfuncs, nnames = struct.unpack_from('<II', data, off + 20)
aofs, anofs = struct.unpack_from('<II', data, off + 32)
names_off = rva2off(anofs)
print('functions', nfuncs, 'names', nnames)
for i in range(nnames):
    s, = struct.unpack_from('<I', data, names_off + i * 4)
    eon = rva2off(s)
    end = data.index(b'\x00', eon)
    print(i, data[eon:end].decode('ascii', 'replace'))
