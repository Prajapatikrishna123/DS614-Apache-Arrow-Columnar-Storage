import pyarrow as pa 
import pyarrow.ipc as ipc 
import os 
schema = pa.schema([pa.field('id', pa.int32())]) 
batch = pa.record_batch({'id': [1,2,3]}, schema=schema) 
with ipc.new_file('corrupt_test.arrow', schema) as w: w.write_batch(batch) 
size = os.path.getsize('corrupt_test.arrow') 
print('Valid file size:', size, 'bytes') 
f = open('corrupt_test.arrow', 'r+b') 
f.seek(0) 
f.write(b'CORRUPT!') 
f.close() 
result = 'success' 
try: 
    ipc.open_file('corrupt_test.arrow').get_batch(0) 
except Exception as e: result = str(e) 
print('Failure 2 confirmed:', result) 
