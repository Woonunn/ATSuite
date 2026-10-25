import csv
from pathlib import Path
import tempfile
import unittest
import numpy as np
from experiments.exp1.profile.lstm_reference import load_training,TRAIN_DATES,FORECAST_DATES


class LSTMInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'data.csv'
        self.rows=[{'Date':d.strftime('%Y-%m-%d'),'Value1':str(i),'Value2':'unused'}
                   for i,d in enumerate(TRAIN_DATES.append(FORECAST_DATES))]

    def write(self):
        with self.path.open('w') as f:
            w=csv.DictWriter(f,fieldnames=['Date','Value1','Value2']);w.writeheader();w.writerows(self.rows)

    def test_exact_training_slice_ignores_future_and_other_columns(self):
        self.write();_,original=load_training(self.path)
        self.assertEqual(len(original),151)
        for row in self.rows[151:]:row['Value1']='1000000000'
        self.write();_,changed=load_training(self.path)
        np.testing.assert_array_equal(original,changed)
        np.testing.assert_array_equal(original,np.arange(151,dtype=np.float32))

    def test_missing_or_duplicate_date_rejected(self):
        saved=list(self.rows)
        self.rows=self.rows[1:];self.write()
        with self.assertRaisesRegex(ValueError,'Missing required'):load_training(self.path)
        self.rows=[*saved,saved[0]];self.write()
        with self.assertRaisesRegex(ValueError,'Duplicate dates'):load_training(self.path)

    def test_nonfinite_or_constant_training_rejected(self):
        for value in ['NaN','inf']:
            with self.subTest(value=value):
                self.rows[0]['Value1']=value;self.write()
                with self.assertRaisesRegex(ValueError,'finite and nonconstant'):load_training(self.path)
        for row in self.rows:row['Value1']='1'
        self.write()
        with self.assertRaisesRegex(ValueError,'finite and nonconstant'):load_training(self.path)


if __name__=='__main__':unittest.main()
