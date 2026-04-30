import pyarrow as pa 
import pyarrow.ipc as ipc 
import os 
schema = pa.schema([pa.field('id', pa.int32()), pa.field('value', pa.float64())]) 
batch = pa.record_batch({'id': list(range(100)), 'value': [float(i) for i in range(100)]}, schema=schema) 
writer = ipc.new_file('crash_test.arrow', schema) 
writer.write_batch(batch) 
del writer 
print('File size:', os.path.getsize('crash_test.arrow'), 'bytes') 
try: 
    r = ipc.open_file('crash_test.arrow') 
    r.get_batch(0) 
except Exception as e: 
    print('Failure 1 confirmed:', e) 
