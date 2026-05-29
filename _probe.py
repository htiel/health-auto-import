import socket, json, time
HOST, PORT = '192.168.1.210', 9000
body = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}) + '\n'
s = socket.socket()
s.settimeout(8)
t0 = time.time()
try:
    s.connect((HOST, PORT))
    print(f'connected in {time.time()-t0:.2f}s')
    s.sendall(body.encode())
    s.settimeout(8)
    data = b''
    while True:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if b'\n' in chunk or len(data) > 200000:
                break
        except socket.timeout:
            print('read timeout')
            break
    print(f'received {len(data)} bytes in {time.time()-t0:.2f}s')
    print(data[:2000].decode(errors='replace'))
except Exception as e:
    print('ERR:', e)
finally:
    s.close()
