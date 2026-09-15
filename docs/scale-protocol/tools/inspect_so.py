import re

def find_strings(path, min_len=4):
    with open(path, 'rb') as f:
        data = f.read()
    strings = []
    curr = []
    for b in data:
        if 32 <= b <= 126:
            curr.append(chr(b))
        else:
            if len(curr) >= min_len:
                strings.append(''.join(curr))
            curr = []
    return strings

s_algo = find_strings('extracted_apk/lib/arm64-v8a/libICBodyFatAlgorithms.so')
print('=== JNI functions in libICBodyFatAlgorithms.so ===')
for s in s_algo:
    if 'Java_' in s:
        print('  ', s)

s_ble = find_strings('extracted_apk/lib/arm64-v8a/libICBleProtocol.so')
print('=== JNI functions in libICBleProtocol.so ===')
for s in s_ble:
    if 'Java_' in s:
        print('  ', s)
