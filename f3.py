import pyarrow as pa 
import pyarrow.ipc as ipc 
import os 
schema = pa.schema([pa.field('id', pa.int32())]) 
batch = pa.record_batch({'id': [1,2,3]}, schema=schema) 
with ipc.new_file('corrupt2.arrow', schema) as w: w.write_batch(batch) 
size = os.path.getsize('corrupt2.arrow') 
print('Valid file size:', size, 'bytes') 
f = open('corrupt2.arrow', 'r+b') 
f.seek(0) 
f.write(b'NOTARROW') 
f.seek(size - 10) 
f.write(b'CORRUPTED') 
f.close() 
result = 'success' 
try: 
    ipc.open_file('corrupt2.arrow').get_batch(0) 
except Exception as e: result = str(e) 
print('Failure 2 result:', result) 
