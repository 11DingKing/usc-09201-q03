# 林地价值证据仓

面向集体林权改革与林下产业协作场景的林地价值证据仓服务：统一接收现场调查、经营方案、收益记录与第三方估值，按估值目的冻结输入快照，生成版本化、可追溯的估值结论，并对发布权限（评估资格有效期、争议双人复核）进行强制校验。

## 运行与测试

```bash
python3 -m unittest          # 全部测试（工作流规则 + HTTP 端到端）
python3 -m service.main      # 启动服务，默认 http://0.0.0.0:3000
curl http://127.0.0.1:3000/health
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/appraisers` | 登记评估师（含资格到期日） |
| POST | `/parcels` | 登记林地 |
| POST | `/parcels/{id}/evidence` | 提交证据（必带 `idempotency_key`；补测追加版本） |
| POST | `/parcels/{id}/market-params` | 修订市场参数（追加版本） |
| POST | `/parcels/{id}/valuations` | 创建估值版本并冻结快照（`purpose/season/idempotency_key`） |
| POST | `/valuations/{id}/retry` | 中断后重算（版本号不变） |
| POST | `/valuations/{id}/dispute` | 标记争议，进入双人复核 |
| POST | `/valuations/{id}/reviews` | 复核人表决（两名不同复核人 approve 方可发布） |
| POST | `/valuations/{id}/publish` | 发布正式结论（校验资格有效期） |
| GET | `/valuations/{id}` / `/trace` | 估值详情 / 完整追溯链 |
| GET | `/snapshots/{id}` | 冻结快照 |
| GET | `/parcels/{id}/conclusion?purpose=` | 当前正式结论 |
| GET | `/parcels/{id}/ledger?purpose=` | 全部估值版本台账 |

## 关键保障

- 同内容重复上传拒绝；幂等键重放与并发同键创建收敛为同一版本。
- 计算中断只把版本标记为 `interrupted`，重试仍基于同一快照，不产生半成品结论。
- 评估资格过期阻止发布；争议值双人复核；每宗林地每个目的唯一正式结论，旧结论保留替代链。

领域规则详见 [`docs/domain.md`](docs/domain.md)。当前存储为进程内内存实现，正式部署应替换为持久化仓储。
