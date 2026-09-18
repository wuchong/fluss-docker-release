<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements. See the NOTICE file
distributed with this work for additional information
regarding copyright ownership. The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied. See the License for the
specific language governing permissions and limitations
under the License.
-->

# 在独立机器上复用 RC 产物构建三个 Docker 镜像

个人维护的 Apache Fluss RC Docker 镜像发布工具。clone 本仓库后即可运行，
无需复制原发布机器的开发工作区。
脚本根据 `--version` 和 `--rc` 推导 `vVERSION-rcN`，从 GitHub 拉取并验证签名 tag，
自动解析其提交号并 checkout，使用该 tag 的 Dockerfile 和 Quickstart 准备脚本。
整个流程不会调用 Maven、Cargo 或 GPG 签名命令，不需要 JDK、Rust 工具链或签名私钥。

## 输入来源

| 镜像 | 复用的产物 |
| --- | --- |
| `apache/fluss:VERSION-rcN` | dist 上的 `fluss-VERSION-bin.tgz` |
| `apache/fluss-gateway:VERSION-rcN` | dist 上的 Gateway Linux amd64 和 arm64 二进制包 |
| `apache/fluss-quickstart-flink:1.20-VERSION-rcN` | Java 二进制包中的 S3、Paimon、Iceberg 插件；同一 RC 的 Flink connector 和 tiering JAR；原 RC 准备脚本指定的第三方依赖 |

**只靠 dist 现有归档不足以生成第三个镜像。** `fluss-flink-1.20-VERSION.jar`
和 `fluss-flink-tiering-VERSION.jar` 不在 Java 二进制包里。
脚本从 `--nexus` 指定的该 RC staging 仓库下载这两个 JAR 和各自的 `.asc` 签名。

JAR 文件名只有版本号，没有 RC 编号。必须使用同一个 RC 的 staging 仓库，
不能从 Maven Central、其他 RC 或另一轮构建中替代。脚本不会自动回退到这些来源。

## 获取脚本

```bash
git clone https://github.com/wuchong/fluss-docker-release.git
cd fluss-docker-release
python3 stage_docker_images.py --help
```

后续更新脚本可在仓库目录执行 `git pull --ff-only`。

## 机器准备

需要 Python 3.9+、Git、curl、GnuPG、bash、`shasum`，以及 Docker 和 Buildx。
Ubuntu/Debian 上 `shasum` 来自 `libdigest-sha-perl`。
机器需要访问 dist.apache.org、GitHub、Docker Hub、Maven Central，以及选用的 Nexus staging 仓库。
公钥自动从 Apache Fluss 的 KEYS 下载，导入本次运行的独立公钥目录。

```bash
docker login -u YOUR_DOCKER_ID
docker buildx create --name fluss-release --driver docker-container --use
docker buildx inspect fluss-release --bootstrap
```

已有可用 builder 时直接使用它，无需重复创建。builder 必须支持 `linux/amd64` 和
`linux/arm64`；Linux 单架构机器需预先配置另一架构的 binfmt/QEMU，或使用多原生节点。
这里模拟执行的是基础镜像内的系统安装命令，Gateway 二进制直接来自已经完成原生编译的 RC 包。
脚本不会创建或删除 builder，也不会更改 Docker 登录或代理配置。

## 执行

以下以 RC3 和 `orgapachefluss-1016` 为例。脚本自动从远程 `v1.0.0-rc3` tag
解析提交号，无需手动填写。其他 RC 需要调整版本、RC 编号及对应的 Nexus staging URL。

```bash
python3 stage_docker_images.py \
  --version 1.0.0 \
  --rc 3 \
  --nexus https://repository.apache.org/content/repositories/orgapachefluss-1016/ \
  --builder fluss-release \
  --work-dir "$PWD/fluss-docker-1.0.0-rc3" \
  --push
```

Nexus 404 不会触发本地重新编译，需要确认仓库编号、上传状态和关闭状态。
不要仅凭早先的部署日志认定仓库当前已公开可下载。

## 校验与运行结果

去掉 `--push` 可先只准备三个构建目录，无需 Docker daemon。
这一步仍会下载 RC 产物、Git tag 和 Quickstart 第三方依赖。

脚本在首次上传镜像之前完成：

1. 从 GitHub 拉取推导出的 RC tag，验证签名并 checkout 到自动解析的提交。
2. 验证三个二进制归档的 SHA-512 与 GPG 签名，以及两个外部 JAR 的 GPG 签名。
3. 校验两个 Gateway 包内的 `RELEASE_COMMIT` 与远程 RC tag 对应提交相同。
4. 从 RC 产物填充所需目录，再执行该 RC 的 Quickstart 准备脚本。
5. 记录构建输入 JAR 的 SHA-512 和将要执行的三个 buildx 命令。

每个镜像使用 `docker buildx build --push --platform linux/amd64,linux/arm64`。
上传后检查远端 manifest 包含两个架构，并核对 RC tag 与刚上传的 index 一致。
默认 namespace 是 `apache`，可通过 `--namespace YOUR_DOCKER_ID` 改到自己的仓库。

`--work-dir` 下保留下载缓存和独立的 `run-*` 目录。不同 RC 或 Nexus 仓库必须换工作目录。
自动解析的 tag 和提交号会记录到 `inputs.json`；同一工作目录重跑时若远程 tag 对应提交改变，会停止。
相同参数重跑会复用已下载的 RC 归档，但会再次验签，并重新准备构建目录和下载第三方依赖。
不会修改调用者的 Git checkout。

每次运行输出：

- `build-commands.sh`：三个镜像的精确构建命令。手动执行会直接上传镜像；完整的远端校验由 Python 脚本执行。
- `prepared-inputs.json`：输入来源与实际放入 Docker context 的 JAR 校验和。
- `*.metadata.json`：各次 Docker buildx 的构建结果。
- `image-digests.txt`：成功上传且通过远端检查的镜像 index digest；正式发布时保留此记录供提升镜像使用。

三个镜像依次上传。如果后续镜像失败，之前已成功上传的镜像及其 digest 记录保留，不会自动删除。
脚本只上传 RC 标签，不生成正式版本或 `latest` 标签。

参考：[官方 Stage Docker images 流程](https://fluss.apache.org/community/how-to-release/creating-a-fluss-release/#7-stage-docker-images)。

## 本地测试

脚本仅使用 Python 标准库，不需要安装 pip 依赖。以下测试无需网络或 Docker：

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

## 许可证

采用 [Apache License 2.0](LICENSE)。本仓库不包含 Fluss 发行归档、第三方 JAR、
Docker 镜像或签名私钥，这些发布产物在运行时从指定来源下载。
