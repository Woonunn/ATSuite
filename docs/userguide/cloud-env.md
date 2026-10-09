# 本地 .env 配置 AWS、阿里云与 Google Cloud

仓库根目录的 [.env.example](../../.env.example) 包含三平台的配置说明。
首次复制用 `cp -n .env.example .env`；已有 `.env` 时保留原文件，按平台追加所需配置。
云配置默认全部注释，避免未使用平台的空凭证、无效路径和示例账号影响真实凭证链。
只取消注释实际使用的选项，替换示例值。

推荐器 CLI、Web、worker 统一加载仓库根目录 `.env`；已有进程变量优先，修改后重启。
`ATSUITE_ENV_FILE` 可指定独立文件，`PYTHON_DOTENV_DISABLED=1` 可禁止加载。
外部 `aws`、`aliyun`、`gcloud` 本身不会读取本项目 `.env`，文中的
`uv run --env-file .env …` 会把配置传给这些命令。

## 先准备运行依赖

```bash
uv sync --locked --group aws --group aliyun --group gcp --group dev
```

实际运行还需要 Docker daemon、buildx、aliyun CLI 和 gcloud CLI。
`.env` 不负责创建 IAM/RAM 角色、启用服务、创建 bucket/registry 或登录 Docker。
本页命令供你配置和检查自己的账号；此次文档更新没有执行认证、部署或创建云资源。

## AWS：Lambda 与 AgentCore

`aws_lambda` 和 `aws_agentcore` 使用同一套本地 AWS 凭证，执行角色分别配置。
按 [Boto3 官方凭证说明](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)，
可使用 profile，或环境变量 AK/SK；STS 凭证还需 `AWS_SESSION_TOKEN`。

**Profile 方式**：先 `aws configure --profile atsuite`，再在 `.env` 启用
`AWS_PROFILE=atsuite`。已有 SSO/role profile 也可使用，先完成对应登录。
此时保留 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_SESSION_TOKEN` 注释。

**环境凭证方式**：填写 `AWS_ACCESS_KEY_ID` 与 `AWS_SECRET_ACCESS_KEY`；临时凭证还需
填写同一会话的 `AWS_SESSION_TOKEN`。不要取消注释却留下空值。

两种方式都需要本仓库使用的部署配置：

```dotenv
AWS_ACCOUNT_ID=123456789012
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
ECR_REPOSITORY_NAME=atsuite
AWS_LAMBDA_ROLE_ARN=arn:aws:iam::123456789012:role/atsuite-lambda-execution-role
AWS_AGENTCORE_ROLE_ARN=arn:aws:iam::123456789012:role/atsuite-agentcore-execution-role
```

替换真实账号、地区和预先建立的角色 ARN。只使用一种运行时可只启用对应角色。
本地身份需要部署/调用、观测、registry 和所需 PassRole 权限；执行角色需要对应服务信任策略、
日志和业务状态访问权限。两者是不同身份。AgentCore 镜像使用 `linux/arm64`。

只读身份检查与 registry 登录示例：

```bash
uv run --env-file .env aws sts get-caller-identity
uv run --env-file .env aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com
```

身份返回的 `Account` 应匹配 `AWS_ACCOUNT_ID` 和审批账号。先建立对应 ECR repository，
再替换登录命令中的地区及 registry 域名。登录方式见
[ECR 官方认证说明](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html)。

## 阿里云：FC 与 FC Session MCP

`ali_fc` 是 FC FaaS；`ali_agentrun` 当前实现为 FC MCP 的 `SESSION_EXCLUSIVE`，
配置这些变量不代表原生 AgentRun 产品已接入。

```dotenv
ALIBABA_CLOUD_ACCESS_KEY_ID=你的AccessKeyID
ALIBABA_CLOUD_ACCESS_KEY_SECRET=你的AccessKeySecret
ALI_ACCOUNT_ID=你的阿里云账号ID
ALI_REGION=cn-hangzhou
ALIBABA_CLOUD_REGION_ID=cn-hangzhou
ALI_ENDPOINT=你的账号ID.cn-hangzhou.fc.aliyuncs.com
DOCKERHUB_NAME=registry.cn-hangzhou.aliyuncs.com/你的命名空间/atsuite
ALI_FC_ROLE_ARN=acs:ram::你的账号ID:role/atsuite-fc-execution-role
```

STS 临时凭证需增加 `ALIBABA_CLOUD_SECURITY_TOKEN`。Python SDK 使用该凭证链；
默认执行预检还通过 aliyun CLI 读取 STS 身份。
[官方 CLI 环境变量说明](https://www.alibabacloud.com/help/zh/cli/environment-variables)
指出 profile 可优先于环境配置。已有 CLI profile 时先确认它与 SDK 的身份一致；
若选择纯环境凭证，可在 `.env` 启用 `ALIBABA_CLOUD_IGNORE_PROFILE=TRUE`。
不要把不同账号的 CLI profile 与 SDK AccessKey 混用。

当前 FC 适配器在传入 region 时使用 `ALI_ACCOUNT_ID` 组装 endpoint；未传 region 的
独立部署路径使用 `ALI_ENDPOINT`。推荐器规格仍须明确 region。
`DOCKERHUB_NAME` 是完整镜像 repository 路径，不是用户名；旧教程中的 `ACR_NAME`
不是当前默认执行链读取的配置名。

预先建立 SLS 项目与 logstore，并启用：

```dotenv
ATSUITE_SLS_PROJECT=atsuite-logs
ATSUITE_SLS_LOGSTORE=atsuite-runtime
ATSUITE_ALI_SLS_PROJECT=atsuite-logs
ATSUITE_ALI_SLS_LOCATION=cn-hangzhou
```

前两项配置 FC 日志目的地，后两项配置采集器默认项目与地区；采集器还会读取 FC 日志配置。
确保运行角色可写日志、本地身份可读 FC/SLS，项目地区与部署相符。
默认推荐器不会向运行时注入 OSS AccessKey，状态访问由运行角色授权。
旧式 `tools.deploy` 非实验路径仍读取 `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET`；
只有使用该路径时才配置这组兼容变量。

```bash
uv run --env-file .env aliyun sts GetCallerIdentity --RegionId cn-hangzhou
docker login --username 你的ACR登录用户名 registry.cn-hangzhou.aliyuncs.com
```

身份的 `AccountId` 应匹配 `ALI_ACCOUNT_ID` 和审批账号。
registry 域名、命名空间和仓库需按你的 ACR 实例替换，并预先建立。

## Google Cloud：Cloud Run FaaS 与 MCP

当前 `gcp_faas` 与 `gcp_mcp` 都由 Cloud Run 承载。

```dotenv
GOOGLE_CLOUD_PROJECT=你的项目ID
GCP_REGION=us-central1
GCP_ARTIFACT_REPOSITORY=atsuite
GCP_BUCKET=你已建立的GCS状态bucket
```

**本地用户登录方式**需要分别设置 CLI 和 SDK 身份：

```bash
gcloud auth login
gcloud auth application-default login
```

CLI 用前者，Python SDK 用后者产生的 ADC；两者是独立配置。
见 [gcloud 官方认证说明](https://docs.cloud.google.com/sdk/docs/authenticate)
和 [ADC 查找顺序](https://docs.cloud.google.com/docs/authentication/application-default-credentials)。

**凭证文件方式**可改为在 `.env` 同时配置：

```dotenv
GOOGLE_APPLICATION_CREDENTIALS=/绝对路径/gcp-credentials.json
CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/绝对路径/gcp-credentials.json
```

前者用于 ADC，后者供 gcloud CLI 使用。文件可为受支持的服务账号凭证或身份联合配置。
只设置 `GOOGLE_APPLICATION_CREDENTIALS` 不会替代 gcloud CLI 的登录配置。
本地用户 ADC 方式应保留这两行注释；不要给未使用的凭证路径填空值。

```bash
uv run --env-file .env gcloud projects describe 你的项目ID --format=json
uv run --env-file .env gcloud auth configure-docker us-central1-docker.pkg.dev
```

默认推荐器发布到
`REGION-docker.pkg.dev/PROJECT/GCP_ARTIFACT_REPOSITORY/runtime`，需预先建立 Artifact Registry repository。
Docker 登录方式见 [Artifact Registry 官方认证说明](https://docs.cloud.google.com/artifact-registry/docs/docker/authentication)。
`GCP_IMAGE_PREFIX` 只影响旧式独立部署的镜像命名，不能替代推荐器的 registry 配置。
本地身份需要 Cloud Run 部署/调用、Artifact Registry、日志/监控和所需运行角色使用权限。
当前部署器没有 `.env` 中自定义 Cloud Run service account 的选项；实际使用的运行身份须获得
对应 GCS 状态读写权限。配置文件不会自动给它授权。

## 状态存储、地区与审批

`ATSUITE_STATE_BUCKETS` 是单行 JSON，按实际 provider 指定已建立的 bucket，例如：

```dotenv
ATSUITE_STATE_BUCKETS='{"aws_lambda":"your-s3-state-bucket","ali_fc":"your-oss-state-bucket","gcp_mcp":"your-gcs-state-bucket"}'
```

默认推荐器部署和回放从该映射选择状态 bucket；`GCP_BUCKET` 同时兼容独立部署路径，
相同 GCP provider 的两项配置应保持一致。默认执行不创建或删除共享 bucket。
`ATSUITE_REGISTRY_REPOSITORIES` 可按 provider 覆盖完整镜像仓库路径，示例见 `.env.example`。
工具含 `init.sh` 时还需设置带真实 SHA256 digest 的 `ATSUITE_INIT_IMAGE`。

环境默认地区不能代替实验规格的明确地区。两个同云 provider 都要分别绑定账号，例如：

```bash
uv run atsuite-recommend run --plan recommendation_results/demo/plan.json \
  --store results/recommender/jobs.sqlite --approve-budget \
  --account aws_lambda=AWS_ACCOUNT_ID --account aws_agentcore=AWS_ACCOUNT_ID \
  --account ali_fc=ALI_ACCOUNT_ID --account ali_agentrun=ALI_ACCOUNT_ID \
  --account gcp_faas=GOOGLE_CLOUD_PROJECT --account gcp_mcp=GOOGLE_CLOUD_PROJECT
```

替换账号/项目 ID，只保留计划中实际使用的 provider，规格 `regions` 也分别填写。
默认预检会从 STS/gcloud 回读真实身份，再与审批绑定比较。
Key、项目 ID 和角色配置完成只表示可开始预检；真实部署、调用、指标采集和清理通过后，
才可更新云验收状态。当前六云验收仍为 `blocked_external`。
