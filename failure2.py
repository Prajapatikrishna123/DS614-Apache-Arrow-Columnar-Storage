import pyarrow as pa 
import pyarrow.ipc as ipc 
import os 
schema = pa.schema([pa.field('id', pa.int32())]) 
batch = pa.record_batch({'id': [1,2,3]}, schema=schema) 
with ipc.new_file('corrupt_test.arrow', schema) as w: w.write_batch(batch) 
print('Valid file size:', os.path.getsize('corrupt_test.arrow'), 'bytes') 
f = open('corrupt_test.arrow', 'r+b') 
f.seek(0) 
f.write(b'CORRUPT!') 
f.close() 
try: 
    r = ipc.open_file('corrupt_test.arrow') 
    r.get_batch(0) 
except Exception as e: 
    print('Failure 2 confirmed:', e) 
