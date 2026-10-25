"""CPU reference for csv_excel_29; only the original Jan–May Value1 enters fitting."""
import hashlib
import json
import numpy as np
import pandas as pd

TRAIN_DATES=pd.date_range('2023-01-01','2023-05-31')
FORECAST_DATES=pd.date_range('2023-06-01','2023-07-19')
PARAMETERS={'lookback':14,'hidden_size':32,'layers':1,'learning_rate':.01,
    'max_epochs':200,'patience':20,'validation_days':28,'min_delta':1e-5,
    'selection':'chronological last 28 training days; scaler fit on earlier training days only',
    'final_fit':'refit selected epoch count on all 151 Jan–May days; scaler refit on these days only',
    'forecast':'49 recursive daily predictions; future actuals excluded from fitting and inference',
    'independent_parallel_subtasks':False,'device':'cpu','torch_version':'2.5.1+cpu'}


def load_training(path):
    d=pd.read_csv(path,usecols=['Date','Value1'])
    d['Date']=pd.to_datetime(d.Date,format='%Y-%m-%d',errors='raise')
    if d.Date.duplicated().any():raise ValueError('Duplicate dates in original data.csv')
    train=d.set_index('Date').sort_index().loc[TRAIN_DATES[0]:TRAIN_DATES[-1]]
    if not train.index.equals(TRAIN_DATES):raise ValueError('Missing required Jan–May daily observations')
    values=pd.to_numeric(train.Value1,errors='raise').to_numpy(dtype=np.float32)
    if not np.isfinite(values).all() or values.std()==0:raise ValueError('Training Value1 must be finite and nonconstant')
    return d,values


def run_reference(c):
    import torch
    import matplotlib.pyplot as plt
    torch.set_num_threads(1);torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.mkldnn.deterministic=True
    c.check('fixed CPU PyTorch environment',torch.__version__=='2.5.1+cpu' and torch.version.cuda is None)
    lookback,hidden=PARAMETERS['lookback'],PARAMETERS['hidden_size']

    class Predictor(torch.nn.Module):
        def __init__(self):
            super().__init__();self.lstm=torch.nn.LSTM(1,hidden,batch_first=True);self.head=torch.nn.Linear(hidden,1)
        def forward(self,x):
            output,_=self.lstm(x);return self.head(output[:,-1,:])

    def model():
        torch.manual_seed(c.seed);return Predictor().cpu()

    def windows(values):
        return (torch.tensor(np.stack([values[i-lookback:i] for i in range(lookback,len(values))]))[:,:,None],
                torch.tensor(values[lookback:])[:,None])

    def weights_hash(m):
        h=hashlib.sha256()
        for name,value in m.state_dict().items():h.update(name.encode());h.update(value.detach().numpy().tobytes())
        return h.hexdigest()

    def rollout(m,window,mean,scale):
        result=[];window=window.clone();m.eval()
        with torch.no_grad():
            for _ in FORECAST_DATES:
                value=m(window[None,:,None]).reshape(())
                result.append(float(value)*scale+mean)
                window=torch.cat([window[1:],value.reshape(1)])
        return np.array(result)

    with c.step('prepare'):
        original,values=load_training(c.inputs/'data.csv')
        c.check('151 required training days',len(values)==151)
        cut=len(values)-PARAMETERS['validation_days'];mu=float(values[:cut].mean());sigma=float(values[:cut].std())
        x,y=windows((values-mu)/sigma);split=cut-lookback
        c.state.update(train_values=values,selection_mean=mu,selection_scale=sigma)
    with c.step('select_epochs'):
        candidate=model();opt=torch.optim.Adam(candidate.parameters(),lr=PARAMETERS['learning_rate'])
        loss_fn=torch.nn.MSELoss();history=[];best=float('inf');best_epoch=0;stale=0
        for epoch in range(PARAMETERS['max_epochs']):
            candidate.train();opt.zero_grad();loss=loss_fn(candidate(x[:split]),y[:split]);loss.backward();opt.step()
            candidate.eval()
            with torch.no_grad():validation=float(loss_fn(candidate(x[split:]),y[split:]))
            history.append({'epoch':epoch+1,'train_loss':float(loss.detach()),'validation_loss':validation})
            if validation<best-PARAMETERS['min_delta']:
                best,best_epoch,stale=validation,epoch+1,0
            else:stale+=1
            if stale>=PARAMETERS['patience']:break
        c.state.update(selection_history=pd.DataFrame(history),selected_epochs=best_epoch)
    with c.step('fit'):
        mean,scale=float(values.mean()),float(values.std());x_full,y_full=windows((values-mean)/scale)
        fitted=model();opt=torch.optim.Adam(fitted.parameters(),lr=PARAMETERS['learning_rate'])
        initial_hash=weights_hash(fitted)
        with torch.no_grad():initial_loss=float(loss_fn(fitted(x_full),y_full))
        for _ in range(best_epoch):
            fitted.train();opt.zero_grad();loss=loss_fn(fitted(x_full),y_full);loss.backward();opt.step()
        fitted.eval()
        with torch.no_grad():final_loss=float(loss_fn(fitted(x_full),y_full))
        checkpoint={'state_dict':fitted.state_dict(),'mean':mean,'scale':scale,
                    'last_window':torch.tensor(((values-mean)/scale)[-lookback:]),
                    'lookback':lookback,'hidden_size':hidden,'seed':c.seed,'selected_epochs':best_epoch,
                    'training_start':'2023-01-01','training_end':'2023-05-31'}
        torch.save(checkpoint,c.path('model_checkpoint.pt'))
        c.state.update(model_state=checkpoint,initial_loss=initial_loss,final_loss=final_loss)
    with c.step('forecast'):
        predictions=rollout(fitted,checkpoint['last_window'],mean,scale)
        c.csv(pd.DataFrame({'Date':FORECAST_DATES.strftime('%Y-%m-%d'),'Predicted_Value1':predictions}),'predictions.csv')
        plt.figure(figsize=(10,4));plt.plot(TRAIN_DATES,values,label='Observed training Value1')
        plt.plot(FORECAST_DATES,predictions,label='LSTM recursive forecast');plt.legend();plt.xlabel('Date');plt.ylabel('Value1');c.plot('predictions_chart.png')
        c.state['predictions']=predictions
    c.check('real LSTM parameters changed through optimization',isinstance(fitted.lstm,torch.nn.LSTM) and weights_hash(fitted)!=initial_hash)
    c.check('training loss decreased',np.isfinite(final_loss) and final_loss<initial_loss)
    disk=pd.read_csv(c.path('predictions.csv'))
    c.check('exactly 49 required daily prediction dates',disk.Date.tolist()==FORECAST_DATES.strftime('%Y-%m-%d').tolist())
    c.check('all forecasts finite',np.isfinite(disk.Predicted_Value1).all())
    saved=torch.load(c.path('model_checkpoint.pt'),map_location='cpu',weights_only=True)
    replay=model();replay.load_state_dict(saved['state_dict'])
    c.check('checkpoint reproduces all saved predictions',np.allclose(rollout(replay,saved['last_window'],saved['mean'],saved['scale']),disk.Predicted_Value1,rtol=0,atol=1e-10))
    # Independently recompute LSTM gates in NumPy for the first prediction.
    state=saved['state_dict'];w=state['lstm.weight_ih_l0'].numpy();u=state['lstm.weight_hh_l0'].numpy()
    b=state['lstm.bias_ih_l0'].numpy()+state['lstm.bias_hh_l0'].numpy();h=np.zeros(hidden);cell=np.zeros(hidden)
    for value in saved['last_window'].numpy():
        gates=w[:,0]*value+u@h+b;i,f,g,o=np.split(gates,4)
        i,f,o=[1/(1+np.exp(-v)) for v in (i,f,o)];cell=f*cell+i*np.tanh(g);h=o*np.tanh(cell)
    independent=float((state['head.weight'].numpy()@h+state['head.bias'].numpy()).item())*scale+mean
    c.check('independent LSTM gate calculation matches first forecast',np.isclose(independent,disk.Predicted_Value1.iloc[0],atol=1e-6))
    # Holdout values are read only here, after predictions are finalized.
    actual=original.set_index('Date').Value1.reindex(FORECAST_DATES).to_numpy(dtype=float)
    evaluation={'forecast_rmse':float(np.sqrt(np.mean((predictions-actual)**2))) if np.isfinite(actual).all() else None,
                'last_observation_baseline_rmse':float(np.sqrt(np.mean((values[-1]-actual)**2))) if np.isfinite(actual).all() else None}
    details={'parameters':PARAMETERS,'seed':c.seed,'training_rows':151,'forecast_rows':49,
        'selected_epochs':best_epoch,'selection_epochs_executed':len(history),'initial_train_mse':initial_loss,
        'final_train_mse':final_loss,'initial_weights_sha256':initial_hash,'trained_weights_sha256':weights_hash(fitted),
        'selection_history':history,'evaluation':evaluation,'evaluation_scope':'holdout actuals used for reporting only, never training, model selection or autoregressive inputs'}
    c.path('training_metrics.json').write_text(json.dumps(details,indent=2,allow_nan=False)+'\n')
