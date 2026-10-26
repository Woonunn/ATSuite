"""Small adversarial self-checks; synthetic fixtures are not benchmark evidence."""
import json,tempfile
from pathlib import Path
import numpy as np
import pandas as pd
from extended_checks import check_mining,items,observe_training,parameter_digest
from sandbox_worker import canonical,artifact


def main():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        basket=pd.DataFrame({'a':[1,1,0,0],'b':[1,1,1,0]},dtype=bool)
        pd.DataFrame([{'itemsets':'["a"]','support':.5},{'itemsets':'["a", "b"]','support':.5}]).to_csv(root/'frequent_itemsets.csv',index=False)
        rules=pd.DataFrame([{'antecedents':'["a"]','consequents':'["b"]','support':.5,'confidence':1.,'lift':4/3}])
        rules.to_csv(root/'association_rules.csv',index=False)
        assert check_mining(root,basket)['all_support_confidence_lift_recomputed']
        rules.loc[0,'confidence']=.5;rules.to_csv(root/'association_rules.csv',index=False)
        try:check_mining(root,basket)
        except AssertionError:pass
        else:raise AssertionError('Tampered confidence passed')
        try:items("__import__('os').system('false')")
        except json.JSONDecodeError:pass
        else:raise AssertionError('Unsafe itemset parser')
        import torch
        records,hooks=observe_training(0);model=torch.nn.Linear(1,1);opt=torch.optim.Adam(model.parameters(),lr=.1)
        for _ in range(3):
            opt.zero_grad();loss=torch.square(model(torch.ones(4,1))-2).mean();loss.backward();opt.step()
        for h in hooks:h.remove()
        record=list(records.values())[0]
        assert record['steps']==3 and record['initial_parameters_sha256']!=record['final_parameters_sha256']
        assert record['final_parameters_sha256']==parameter_digest(model.parameters())
        assert canonical(torch.tensor([1.,2.]))==canonical(np.array([1.,2.],dtype=np.float32))
        for name in ('a.pt','different-path.pt'):torch.save({'state_dict':model.state_dict()},root/name)
        assert artifact(root/'a.pt',root)['signature']==artifact(root/'different-path.pt',root)['signature'],'Checkpoint signature depends on archive filename'
    print('PASS: numeric mining checks, tampering rejection, JSON-only parser, actual optimizer updates, tensor/checkpoint signatures')


if __name__=='__main__':main()
