# 林地价值证据仓

城口县金融服务中心林地价值证据仓：统一接收现场调查、经营方案、收益
记录与第三方估值，按估值目的冻结输入快照并生成可追溯估值结论，供银行
融资复核、评估机构出证与林农融资使用。

## 能力

- **证据不可变 + 自动去重**：四类证据按业务指纹识别，重复上传与离线
  补传不会产生重复材料。
- **市场参数版本化**：连续修订只追加新版本；林下作物、道路可达性、
  管护义务等季节变化通过补测证据与新参数形成新版本，旧结论原样保留。
- **冻结快照 + 纯函数计算**：每版价值引用证据指纹集合、参数快照与
  公式版本（`forest-income-v1`），可逐项复算与解释差异。
- **发布闸门**：评估资格过期阻止发布；争议值双人独立复核（编制人
  不得复核本人版本）；计算中断可安全重试，不落部分结果。
- **唯一正式结论**：同一估值目的至多一份 PUBLISHED 结论；并发发布、
结论编号冲突、幂等键重发都不会制造两份结论。

## 运行

```bash
python3 -m unittest discover -s tests   # 15 个测试
PORT=3000 python3 -m service.main       # 访问 /health
```

## HTTP 接口

写接口支持 `Idempotency-Key` 请求头；身份通过 `X-User-Id` 与
`X-User-Roles`（appraiser / reviewer / publisher）传递。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/parcels` | 登记宗地 |
| POST | `/parcels/<pid>/evidence` | 登记证据（自动去重） |
| GET | `/parcels/<pid>/evidence` | 列出全部历史证据 |
| POST | `/parcels/<pid>/market-parameters` | 追加市场参数版本 |
| POST | `/parcels/<pid>/purposes/<purpose>/versions` | 冻结快照、生成版本 |
| GET | `/parcels/<pid>/purposes/<purpose>/versions` | 版本序列（含各版价值与引用） |
| GET | `/parcels/<pid>/purposes/<purpose>/conclusion` | 当前正式结论 |
| POST | `/versions/<vid>/calculate` | 价值计算（中断可重试） |
| POST | `/versions/<vid>/reviews` | 双人复核意见 |
| POST | `/versions/<vid>/publish` | 发布正式结论 |
| GET | `/versions/<vid>` | 版本详情 |

领域规则见 [docs/domain.md](docs/domain.md)。
