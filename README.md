# 充电设备降级容量核算

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q service_09251_007 tests
```
