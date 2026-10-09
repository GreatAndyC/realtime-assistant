# DeepSeek-V4 论文摘要资料

来源：[DeepSeek-AI，*DeepSeek-V4: Towards Highly Efficient Million-Token Context Intelligence*，arXiv:2606.19348](https://arxiv.org/abs/2606.19348)。本文件是根据论文摘要整理的中文检索样例，不代替论文全文。

- DeepSeek-V4-Pro 和 DeepSeek-V4-Flash 都支持 **一百万 token 的上下文**。
- 长上下文效率来自结合压缩稀疏注意力（CSA）与高度压缩注意力（HCA）的混合注意力架构。
- 论文还提出流形约束超连接（mHC），并采用 Muon 优化器，分别用于改进残差连接、训练收敛速度与稳定性。
- 在一百万 token 的设置下，论文报告 DeepSeek-V4-Pro 的单 token 推理 FLOPs 为 DeepSeek-V3.2 的 27%，KV 缓存为 10%。这些是论文报告的比较结果，不是本项目实测。
