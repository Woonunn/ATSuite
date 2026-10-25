"""Human-authored implementations of original prompts, executed only inside ATSuite Tool.

These are feasibility/profiling references, not captured LLM agent trajectories.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import pickle
import resource
import threading
import time
from datetime import datetime, timezone

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psutil
from sklearn.cluster import KMeans
from sklearn.ensemble import RandomForestClassifier
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
from mlxtend.frequent_patterns import apriori, association_rules
from reportlab.pdfgen.canvas import Canvas
from pypdf import PdfReader

OUTPUTS = {
 'csv_excel_29':['predictions.csv','predictions_chart.png'],
 'human_12':['cleaned_data.csv','model_predictions.csv','model_checkpoint.pkl','clustering_results.csv','analysis_report.pdf'],
 'csv_excel_34':['cleaned_data.csv','lorenz_curve.png','gini_coefficient.txt'],
 'human_142':['association_rules.csv','frequent_itemsets.csv','kmeans_clusters.csv','elbow_method.png','pattern_analysis_report.pdf','client_segments_analysis.pdf'],
 'human_7':['distribution_by_country.png','sales_trends_over_time.png','correlation_heatmap.png','frequent_itemsets.csv','popular_products_analysis.csv','comprehensive_report.pdf'],
 'human_15':['bar_chart.png','line_chart.png','scatter_chart.png'],
}
DAG = {
 'csv_excel_29':{'prepare':[],'select_epochs':['prepare'],'fit':['select_epochs'],'forecast':['fit']},
 'human_12':{'prepare':[], 'predict':['prepare'], 'cluster':['prepare'], 'report':['predict','cluster']},
 'csv_excel_34':{'prepare':[], 'lorenz':['prepare'], 'gini':['prepare']},
 'human_142':{'prepare':[], 'mine':['prepare'], 'cluster_elbow':['prepare'], 'reports':['mine','cluster_elbow']},
 'human_7':{'prepare':[], 'visualize':['prepare'], 'mine':['prepare'], 'products':['prepare'], 'report':['visualize','mine','products']},
 'human_15':{'prepare':[], 'bar':['prepare'], 'line':['prepare'], 'scatter':['prepare']},
}
from experiments.exp1.profile.lstm_reference import PARAMETERS as LSTM_PARAMETERS

PARAMETERS = {
 'csv_excel_29':LSTM_PARAMETERS,
 'human_12':{'random_forest_trees':200,'kmeans_clusters':6,'n_init':10,'holdout':'official adult.test; fit all preprocessing on adult.data only'},
 'human_142':{'elbow_k':list(range(2,9)),'n_init':10,'apriori_min_support':.05,'max_itemset_length':3,'min_confidence':.6},
 'human_7':{'apriori_min_support':.03,'max_itemset_length':3,'min_confidence':.5,'scope':'both years; all countries; positive quantity/price, noncancelled invoices'},
 'csv_excel_34':{'cleaning':'drop NA/negative income; preserve nonnegative observations'},
 'human_15':{'input':'exact CSV embedded in original Prompt'},
}


def digest(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()


class Context:
    def __init__(self, task, inputs, out, seed):
        self.task,self.inputs,self.out,self.seed=task,Path(inputs),Path(out),seed
        self.out.mkdir(parents=True,exist_ok=True)
        self.state={};self.steps=[];self.checks=[];self.figures=[]

    def path(self, name): return self.out/name

    def csv(self, df, name, **kw): df.to_csv(self.path(name),index=False,**kw)

    def plot(self, name):
        plt.tight_layout();plt.savefig(self.path(name),dpi=120);plt.close();self.figures.append(name)

    def check(self, name, condition):
        if not bool(condition): raise AssertionError(name)
        self.checks.append(name)

    @contextlib.contextmanager
    def step(self, name):
        stop=threading.Event();process=psutil.Process();peak=[process.memory_info().rss]
        def sample():
            while not stop.wait(.01):
                peak[0]=max(peak[0],process.memory_info().rss)
        thread=threading.Thread(target=sample,daemon=True);thread.start()
        before=resource.getrusage(resource.RUSAGE_SELF);start=time.perf_counter()
        utc=datetime.now(timezone.utc).isoformat();status='ok'
        try:
            yield
        except BaseException:
            status='failed';raise
        finally:
            elapsed=time.perf_counter()-start;after=resource.getrusage(resource.RUSAGE_SELF)
            peak[0]=max(peak[0],process.memory_info().rss);stop.set();thread.join()
            cpu=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime
            serial_start=time.perf_counter();blob=pickle.dumps(self.state,protocol=5)
            row={'run_id':os.environ['ATSUITE_PROFILE_RUN_ID'],'request_id':os.environ['ATSUITE_PROFILE_RUN_ID']+'-tool','step':name,'status':status,'utc':utc,'wall_seconds':elapsed,'cpu_seconds':cpu,
                 'cpu_percent_one_core':100*cpu/elapsed,'cpu_percent_allocated_two_cores':50*cpu/elapsed,'sampled_peak_rss_bytes':peak[0],
                 'shared_state_bytes':len(blob),'shared_state_sha256':hashlib.sha256(blob).hexdigest(),
                 'state_serialization_seconds':time.perf_counter()-serial_start}
            self.steps.append(row)
            with (self.out.parent/'events.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
            del blob


def report(ctx,name,title,lines,figures=()):
    c=Canvas(str(ctx.path(name)),invariant=1);c.setTitle(title)
    c.setFont('Helvetica-Bold',14);c.drawString(35,800,title)
    y=775;c.setFont('Helvetica',9)
    for line in lines:
        # Wrap data-derived prose rather than silently clipping report content.
        import textwrap
        for part in textwrap.wrap(str(line),100):
            if y<45:c.showPage();c.setFont('Helvetica',9);y=800
            c.drawString(35,y,part);y-=13
    for figure in figures:
        c.showPage();c.drawImage(str(ctx.path(figure)),30,200,width=535,height=450,preserveAspectRatio=True)
    c.save()


def metrics(y,pred,prob):
    return {'accuracy':accuracy_score(y,pred),'precision':precision_score(y,pred),
            'recall':recall_score(y,pred),'f1':f1_score(y,pred),'roc_auc':roc_auc_score(y,prob)}


def adult(c):
    names='age workclass fnlwgt education education_num marital_status occupation relationship race sex capital_gain capital_loss hours_per_week native_country income'.split()
    with c.step('prepare'):
        frames=[pd.read_csv(c.inputs/n,header=None,names=names,skipinitialspace=True,na_values='?',comment='|') for n in ('adult.data','adult.test')]
        for d in frames:d['income']=d.income.str.rstrip('.');d.dropna(subset=['income'],inplace=True)
        train,test=frames;X=train.drop(columns='income');Xt=test.drop(columns='income')
        numeric=X.select_dtypes('number').columns.tolist();categorical=[x for x in X if x not in numeric]
        pre=ColumnTransformer([('num',make_pipeline(SimpleImputer(strategy='median'),StandardScaler()),numeric),
             ('cat',make_pipeline(SimpleImputer(strategy='most_frequent'),OneHotEncoder(handle_unknown='ignore',sparse_output=False)),categorical)])
        a=pre.fit_transform(X);b=pre.transform(Xt);y=(train.income=='>50K').astype(int).to_numpy();yt=(test.income=='>50K').astype(int).to_numpy()
        cleaned=pd.DataFrame(np.vstack([a,b]),columns=pre.get_feature_names_out());cleaned['income_gt_50k']=np.r_[y,yt]
        cleaned['split']=['train']*len(a)+['test']*len(b);c.csv(cleaned,'cleaned_data.csv')
        c.state.update(train=a,test=b,y=y,yt=yt,preprocessor=pre,cleaned=cleaned)
        c.check('official train/test sizes',len(a)==32561 and len(b)==16281)
        c.check('no missing cleaned values',not cleaned.isna().any().any())
    with c.step('predict'):
        model=RandomForestClassifier(n_estimators=200,n_jobs=1,random_state=c.seed)
        model.fit(a,y);prob=model.predict_proba(b)[:,1];pred=model.predict(b);scores=metrics(yt,pred,prob)
        result=pd.DataFrame({'actual':yt,'predicted':pred,'probability':prob})
        for key,value in scores.items():result[key]=value
        c.csv(result,'model_predictions.csv')
        with c.path('model_checkpoint.pkl').open('wb') as f:pickle.dump({'preprocessor':pre,'model':model},f,protocol=5)
        c.state.update(model=model,metrics=scores)
    with c.step('cluster'):
        allx=np.vstack([a,b]);labels=KMeans(n_clusters=6,n_init=10,random_state=c.seed).fit_predict(allx)
        clusters=pd.DataFrame({'cluster':labels,'income_gt_50k':np.r_[y,yt],
            'age':np.r_[train.age,test.age],'hours_per_week':np.r_[train.hours_per_week,test.hours_per_week]})
        clusters['cluster_mean_age']=clusters.groupby('cluster').age.transform('mean')
        clusters['cluster_mean_hours']=clusters.groupby('cluster').hours_per_week.transform('mean')
        clusters['cluster_income_rate']=clusters.groupby('cluster').income_gt_50k.transform('mean')
        c.csv(clusters,'clustering_results.csv');c.state['clusters']=clusters
    with c.step('report'):
        plt.figure();pd.Series(scores).plot.bar(ylim=(0,1));c.plot('model_metrics.png')
        plt.figure();clusters.groupby('cluster').income_gt_50k.mean().plot.bar();c.plot('cluster_income_rates.png')
        plt.figure();train.age.hist(bins=30);plt.xlabel('Age');c.plot('adult_age.png')
        report(c,'analysis_report.pdf','Adult Income Analysis',[
          'Official split: train 32561 / held-out test 16281; no target leakage.',
          'Median numeric imputation, categorical most-frequent imputation, standard scaling and one-hot encoding fit on training data only.',
          'Random forest 200 trees, seed '+str(c.seed), 'Held-out metrics: '+json.dumps(scores),
          'EDA age mean/median/std: '+str(train.age.agg(['mean','median','std']).to_dict()),
          'Missing values before imputation: '+str(sum(d.isna().sum().sum() for d in frames)),
          'Anomaly/imbalance: high-income target fraction '+str(float(y.mean())),
          'Cluster profiles (size, income, age and working hours): '+clusters.groupby('cluster').agg(size=('age','count'),income_rate=('income_gt_50k','mean'),age=('age','mean'),hours=('hours_per_week','mean')).to_json(),
          'Observed association is descriptive; cluster income rates vary and do not establish causality.'],c.figures)
    # Independent checks use persisted outputs and restored estimator.
    with c.path('model_checkpoint.pkl').open('rb') as f:saved=pickle.load(f)
    disk=pd.read_csv(c.path('model_predictions.csv'));p=saved['model'].predict_proba(saved['preprocessor'].transform(Xt))[:,1]
    c.check('checkpoint reproduces all held-out probabilities',np.allclose(p,disk.probability))
    c.check('held-out accuracy >= .80',float((disk.actual==disk.predicted).mean())>=.8)
    c.check('all five metrics recompute from saved predictions',all(np.isclose(disk[k].iloc[0],v) for k,v in metrics(disk.actual,disk.predicted,disk.probability).items()))
    persisted=pd.read_csv(c.path('clustering_results.csv'))
    c.check('cluster profiles equal actual label income rates',np.allclose(persisted.cluster_income_rate,persisted.groupby('cluster').income_gt_50k.transform('mean')) and persisted.cluster.nunique()==6)


def census(c):
    with c.step('prepare'):
        raw=pd.read_csv(c.inputs/'data_pums_2000.csv');d=raw.dropna(subset=['AGE','INCTOT']);d=d[(d.AGE>=0)&(d.INCTOT>=0)].copy()
        c.csv(d,'cleaned_data.csv');x=np.sort(d.INCTOT.to_numpy(dtype=float));c.state.update(data=d,incomes=x)
        c.check('authentic census schema and nontrivial population',list(raw.columns)==['AGE','INCTOT'] and len(raw)>10000)
    with c.step('lorenz'):
        lorenz=np.r_[0,np.cumsum(x)/x.sum()];population=np.linspace(0,1,len(lorenz))
        plt.figure();plt.plot(population,lorenz,label='Lorenz');plt.plot([0,1],[0,1],'--',label='Equality');plt.legend();c.plot('lorenz_curve.png')
        c.state['lorenz']=lorenz
    with c.step('gini'):
        g=float(2*np.dot(np.arange(1,len(x)+1),x)/(len(x)*x.sum())-(len(x)+1)/len(x))
        c.path('gini_coefficient.txt').write_text(f'{g:.12f}\n')
    c.check('Lorenz integration independently matches rank Gini',np.isclose(float(c.path('gini_coefficient.txt').read_text()),1-2*np.trapz(lorenz,population),atol=1e-11))
    c.check('Gini lies between zero and one',0<=g<=1)
    c.check('CSV preserves all valid observed incomes',pd.read_csv(c.path('cleaned_data.csv')).equals(d.reset_index(drop=True)))


def mine(c,basket,support,confidence):
    frequent=apriori(basket,min_support=support,use_colnames=True,max_len=3,low_memory=True)
    rules=association_rules(frequent,metric='confidence',min_threshold=confidence)
    def strings(frame,columns):
        frame=frame.copy()
        for col in columns:frame[col]=frame[col].map(lambda x:json.dumps(sorted(x),ensure_ascii=False))
        return frame.sort_values(columns).reset_index(drop=True)
    c.csv(strings(frequent,['itemsets']),'frequent_itemsets.csv')
    c.csv(strings(rules,['antecedents','consequents']),'association_rules.csv')
    c.state.update(frequent=frequent,rules=rules)


def verify_mining(c,basket):
    c.check('nonempty frequent itemsets and rules',len(c.state['frequent'])>0 and len(c.state['rules'])>0)
    # Check every persisted itemset/rule directly against original baskets, separately from Apriori.
    persisted=pd.read_csv(c.path('frequent_itemsets.csv'))
    c.check('all itemset supports match transaction counts',all(np.isclose(basket[json.loads(r.itemsets)].all(axis=1).mean(),r.support) for r in persisted.itertuples()))
    persisted_rules=pd.read_csv(c.path('association_rules.csv'))
    c.check('all rule confidences match transaction counts',all(np.isclose(basket[json.loads(r.antecedents)+json.loads(r.consequents)].all(axis=1).sum()/basket[json.loads(r.antecedents)].all(axis=1).sum(),r.confidence) for r in persisted_rules.itertuples()))


def credit(c):
    with c.step('prepare'):
        d=pd.read_excel(c.inputs/'default of credit card clients.xls',header=1).rename(columns={'default payment next month':'default'})
        c.check('30000 original clients and complete features',len(d)==30000 and not d.isna().any().any())
        features=d.drop(columns=['ID','default']);x=StandardScaler().fit_transform(features)
        # Fixed, interpretable bins for association mining, no rows sampled or replicated.
        discrete=d[['SEX','EDUCATION','MARRIAGE','PAY_0','default']].astype(str)
        discrete['AGE']=pd.cut(d.AGE,[0,30,40,50,150]).astype(str)
        discrete['LIMIT_BAL']=pd.cut(d.LIMIT_BAL,[0,50000,150000,300000,np.inf]).astype(str)
        basket=pd.get_dummies(discrete,prefix_sep='=',dtype=bool);c.state.update(data=d,scaled=x,baskets=basket)
    with c.step('mine'):mine(c,basket,.05,.6)
    with c.step('cluster_elbow'):
        models=[KMeans(n_clusters=k,n_init=10,random_state=c.seed).fit(x) for k in range(2,9)]
        inertia=np.array([m.inertia_ for m in models]);ks=np.arange(2,9)
        # Knee = farthest point below normalized chord; all fitted models are reused.
        curve=(inertia-inertia[-1])/(inertia[0]-inertia[-1]);distance=(1-(ks-2)/6)-curve
        best=int(np.argmax(distance));model=models[best];labels=model.labels_
        c.csv(d.assign(cluster=labels),'kmeans_clusters.csv')
        c.csv(pd.DataFrame({'k':ks,'inertia':inertia,'chord_distance':distance}),'elbow_values.csv')
        plt.figure();plt.plot(ks,inertia,'o-');plt.axvline(ks[best],linestyle='--');plt.xlabel('k');plt.ylabel('Within-cluster sum of squares');c.plot('elbow_method.png')
        c.state.update(model=model,inertia=inertia,labels=labels)
    with c.step('reports'):
        profiles=d.assign(cluster=labels).groupby('cluster').agg(clients=('ID','count'),default_rate=('default','mean'),credit_limit=('LIMIT_BAL','mean'),age=('AGE','mean'),bill_amount=('BILL_AMT1','mean'),repayment_status=('PAY_0','mean'))
        c.csv(profiles.reset_index(),'cluster_profiles.csv')
        risk=d.groupby('PAY_0').default.agg(['count','mean'])
        report(c,'pattern_analysis_report.pdf','Credit Default Patterns',[
          '30000 observed clients; all rows used. Frequent itemsets support >= .05, up to 3 features; rule confidence >= .6.',
          'Interpretable bins: sex, education, marriage, payment status, default, age, credit limit.',
          'Default prevalence '+str(d.default.mean()),'Default by latest repayment status: '+risk.to_json(),
          'Association rules: '+str(len(c.state['rules'])),'Strongest default-related rules: '+str(c.state['rules'][c.state['rules'].consequents.map(lambda s:'default=1' in s)].nlargest(5,'lift').to_dict('records')),
          'Outliers: negative bill amounts '+str(int((d.filter(regex='BILL_AMT')<0).any(axis=1).sum()))+'. They are retained as observed credit balances.',
          'Association is not causation; patterns describe this dataset.'])
        report(c,'client_segments_analysis.pdf','Credit Client Segments',[
          'Standardized all 23 predictors; default label excluded from fitting.',
          f'Elbow selected k={model.n_clusters}: maximum normalized distance below endpoint chord on k=2..8.',
          'Cluster profiles: '+profiles.to_json(),'Higher observed cluster default rates identify descriptive risk groups, not calibrated future risk.'],['elbow_method.png'])
    verify_mining(c,basket)
    saved=pd.read_csv(c.path('kmeans_clusters.csv'));profiles=pd.read_csv(c.path('cluster_profiles.csv'))
    c.check('all clients clustered and nonempty segments',len(saved)==30000 and saved.cluster.nunique()==model.n_clusters)
    c.check('cluster risk profile matches persisted observations',np.allclose(profiles.default_rate,saved.groupby('cluster').default.mean()))
    ev=pd.read_csv(c.path('elbow_values.csv'))
    c.check('selected elbow recomputes from actual inertia',model.n_clusters==int(ev.loc[ev.chord_distance.idxmax(),'k']))


def retail(c):
    with c.step('prepare'):
        sheets=pd.read_excel(c.inputs/'online_retail_II.xlsx',sheet_name=None)
        raw=pd.concat(sheets.values(),ignore_index=True)
        c.check('both authentic retail years loaded',len(raw)==1067371 and len(sheets)==2)
        d=raw[(raw.Quantity>0)&(raw.Price>0)&~raw.Invoice.astype(str).str.startswith('C')].copy()
        d['InvoiceDate']=pd.to_datetime(d.InvoiceDate);d['revenue']=d.Quantity*d.Price
        d['basket_id']=d.InvoiceDate.dt.year.astype(str)+':'+d.Invoice.astype(str)
        d['StockCode']=d.StockCode.astype(str);c.state.update(data=d,source_rows=len(raw))
        del raw,sheets
    with c.step('visualize'):
        country=d.groupby('Country').size().sort_values(ascending=False)
        trend=d.set_index('InvoiceDate').revenue.resample('MS').sum()
        corr=d[['Quantity','Price','revenue']].corr()
        plt.figure(figsize=(11,6));country.plot.bar();plt.ylabel('Transaction lines');c.plot('distribution_by_country.png')
        plt.figure();trend.plot();plt.ylabel('Sales revenue');c.plot('sales_trends_over_time.png')
        plt.figure();plt.imshow(corr,vmin=-1,vmax=1,cmap='coolwarm');plt.colorbar();plt.xticks(range(3),corr.columns);plt.yticks(range(3),corr.columns);c.plot('correlation_heatmap.png')
        c.csv(country.rename('transactions').reset_index(),'country_counts.csv');c.csv(trend.rename('revenue').reset_index(),'monthly_sales.csv')
    with c.step('mine'):
        pairs=d[['basket_id','StockCode']].drop_duplicates();total=pairs.basket_id.nunique()
        counts=pairs.StockCode.value_counts();eligible=counts[counts/total>=.03].index
        basket=pd.crosstab(pairs.loc[pairs.StockCode.isin(eligible),'basket_id'],pairs.loc[pairs.StockCode.isin(eligible),'StockCode']).reindex(pairs.basket_id.unique(),fill_value=0).gt(0)
        # Pruning infrequent singletons is Apriori's exact downward-closure optimization.
        c.state['baskets']=basket;mine(c,basket,.03,.5)
    with c.step('products'):
        products=d.groupby('StockCode').agg(quantity=('Quantity','sum'),revenue=('revenue','sum'),invoices=('basket_id','nunique'),customers=('Customer ID','nunique')).sort_values('quantity',ascending=False)
        c.csv(products.reset_index(),'popular_products_analysis.csv');c.state['products']=products
        c.check('product revenue conserved',np.isclose(products.revenue.sum(),d.revenue.sum()))
    with c.step('report'):
        customer=d.groupby('Customer ID').agg(orders=('basket_id','nunique'),revenue=('revenue','sum'))
        report(c,'comprehensive_report.pdf','Online Retail II Analysis',[
          f'Both sheets: {c.state["source_rows"]} source rows; {len(d)} positive, noncancelled line items; all countries.',
          'Country distribution counts transaction lines. Revenue is Quantity times Price; monthly trend uses invoice timestamp.',
          'Missing customer IDs retained for sales and basket mining, excluded from customer-specific grouping.',
          f'Total revenue {d.revenue.sum():.2f}; invoices {d.basket_id.nunique()}; known customers {len(customer)}.',
          'Returning customer fraction '+str(float((customer.orders>1).mean())),
          'Highest quantity products: '+products.head(8).to_json(),
          f'Apriori: support >= .03, max length 3; rules confidence >= .5. Itemsets {len(c.state["frequent"])}; rules {len(c.state["rules"])}.',
          'Example association rules: '+str(c.state['rules'].nlargest(5,'lift').to_dict('records')),
          'Seasonality and concentrated country sales suggest inventory planning opportunities; correlations and associations are descriptive.'],c.figures)
    verify_mining(c,basket)
    c.check('country counts equal all retained lines',pd.read_csv(c.path('country_counts.csv')).transactions.sum()==len(d))
    c.check('monthly revenue conserves transaction revenue',np.isclose(pd.read_csv(c.path('monthly_sales.csv')).revenue.sum(),d.revenue.sum()))
    persisted=pd.read_csv(c.path('popular_products_analysis.csv'))
    sample=persisted.iloc[0];observed=d[d.StockCode==str(sample.StockCode)]
    c.check('most popular product quantity and buyers independently recompute',sample.quantity==observed.Quantity.sum() and sample.customers==observed['Customer ID'].nunique())


def sales(c):
    with c.step('prepare'):
        d=pd.read_csv(c.inputs/'sales.csv');c.state['data']=d
        c.check('exact embedded sales totals',d.groupby('Year').Sales.sum().to_dict()=={2022:4800,2023:7800})
    with c.step('bar'):
        fig,ax=plt.subplots();totals=d.groupby('Year').Sales.sum();ax.bar(totals.index.astype(str),totals,color='red',edgecolor='black',width=.7)
        ax.set_xlabel('Year');ax.set_ylabel('Sales');c.check('bar heights equal yearly totals',np.array_equal([p.get_height() for p in ax.patches],totals.to_numpy()));c.plot('bar_chart.png')
    with c.step('line'):
        months=['January','February','March','April','May','June','July','August','September','October','November','December']
        fig,ax=plt.subplots()
        for category,group in d.groupby('Category'):
            values=group.groupby('Month').Sales.sum().reindex(months,fill_value=0)
            ax.plot(months,values,'-.',label=category)
        ax.tick_params(axis='x',rotation=60);ax.legend();c.check('dash-dot line per category',len(ax.lines)==3 and all(x.get_linestyle()=='-.' for x in ax.lines));c.plot('line_chart.png')
    with c.step('scatter'):
        fig,ax=plt.subplots();times=pd.to_datetime(d.Year.astype(str)+'-'+d.Month,format='%Y-%B')
        for category,g in d.groupby('Category'):ax.scatter(times.loc[g.index],g.Sales,s=g.Sales/10,alpha=.5,label=category)
        ax.legend();ax.set_xlabel('Time');ax.set_ylabel('Sales');c.check('one plotted point for each real observation',sum(len(x.get_offsets()) for x in ax.collections)==7);c.plot('scatter_chart.png')


def lstm(c):
    from experiments.exp1.profile.lstm_reference import run_reference
    run_reference(c)


FUNCTIONS={'csv_excel_29':lstm,'human_12':adult,'csv_excel_34':census,'human_142':credit,'human_7':retail,'human_15':sales}


def run(task,inputs,out,seed=0):
    c=Context(task,inputs,out,seed);status='ok';error=None;start=time.perf_counter()
    try:
        FUNCTIONS[task](c)
        for name in OUTPUTS[task]:
            p=c.path(name);c.check('nonempty expected output '+name,p.is_file() and p.stat().st_size>0)
            if p.suffix=='.png':
                from PIL import Image
                with Image.open(p) as im:im.verify()
                c.check('valid PNG '+name,p.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            if p.suffix=='.pdf':
                pdf=PdfReader(p);c.check('readable PDF with actual report content '+name,len(pdf.pages)>0 and sum(len(page.extract_text() or '') for page in pdf.pages)>100)
    except BaseException as e:
        import traceback
        status='failed';error={'type':type(e).__name__,'message':str(e),'traceback':traceback.format_exc()}
    record={'task_id':task,'seed':seed,'status':status,'error':error,'validation':c.checks,'steps':c.steps,
       'total_reference_wall_seconds':time.perf_counter()-start,
       'compute_wall_seconds':sum(s['wall_seconds'] for s in c.steps),
       'cpu_seconds':sum(s['cpu_seconds'] for s in c.steps),
       'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
       'shared_state_encoding':'actual live reference state serialized with pickle protocol 5; not SDK JSON or a FaaS storage cost',
       'max_shared_state_bytes':max((s['shared_state_bytes'] for s in c.steps),default=None),
       'workspace_bytes':sum(p.stat().st_size for p in c.out.rglob('*') if p.is_file()),
       'dag':DAG[task],'parameters':PARAMETERS[task],
       'outputs':[{'name':p.name,'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(c.out.iterdir()) if p.is_file()]}
    (c.out.parent/'profile.json').write_text(json.dumps(record,indent=2,allow_nan=False)+'\n')
    if status!='ok':raise RuntimeError(error['traceback'])
    print(json.dumps({'task':task,'status':status,'compute_seconds':record['compute_wall_seconds']}))
