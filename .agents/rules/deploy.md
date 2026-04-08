---
trigger: always_on
---

你完成功能开发之后你默认只更新开发环境，每天我会有一个固定的时间窗口更新生产环境，需要更新生产环境的时候，我会跟你说。所以，当你在任何情况下准备更新生产环境之前都需要让我明确确认。

## 分支管理与代码流转

### 1. 分支策略
- **大功能开发**：新开独立的 feature 分支（例如 `feat-xxx`）。
- **日常小功能与 Bug 修复**：在 `enhance` 分支上进行开发。
- **开发与测试发布**：使用 `release` 分支进行归集，**开发环境 (dev) 默认部署此分支**。
- **生产发布**：`main` 分支。仅用于最终发版与生产环境 (prod) 更新，**严禁直接修改 main 分支**。

### 2. 测试与合并流程
以日常开发为例：
1. 在 `enhance` 分支完成开发和本地自测，并 Push。
2. 将 `enhance` 合并到 `release` 分支 (`git checkout release && git merge enhance && git push`)。
3. 在**开发环境**拉取 `release` 分支并在界面上进行测试验证。
4. **开发环境测试无误后**，将 `enhance` 的内容合并到 `main` 分支 (`git checkout main && git merge enhance && git push`)，等待后续的生产环境发版。

## 服务器部署操作（两台都用 Docker）

#### 生产环境
- 地址：82.156.53.84，SSH: `ssh -p 10022 qinrui@82.156.53.84`，密码：`Clawith-SFGT`
- Clawith admin 邮箱秘密：qinrui@clawith.ai / Clawith123456
- 前端端口 3008，通过 129.226.64.9 中转到 try.clawith.ai
- Clawith 目录：`/home/qinrui/Clawith`
- 目标分支：`main`

#### 开发环境
- 地址：192.168.106.163，SSH: `ssh root@192.168.106.163`，密码：`dataelem`
- Clawith admin 邮箱密码：you@dataelem.com / admin123
- 前端端口 3008（内网），外网映射：`110.16.193.170:51112`（公网 IP:转发端口）
- `PUBLIC_BASE_URL=http://110.16.193.170:51112`（已写入 dev 服务器的 `.env`）
- Clawith 目录：`/home/work/Clawith`
- 目标分支：`release`
- # 注意：dev 服务器无法直接访问 GitHub，git pull 需使用 ghfast 代理：
- git remote set-url origin https://ghfast.top/https://github.com/dataelement/Clawith.git

#### 更新步骤（两台服务器相同）：
```bash
cd <Clawith目录>
git stash 2>/dev/null
# 开发环境执行 git pull origin release
# 生产环境执行 git pull origin main
git pull origin <分支名>

# 如有前端变更，在服务器上构建前端：
cd frontend && rm -rf dist node_modules/.vite && npm install && npm run build 
cp public/logo.png dist/ && cp public/logo.svg dist/ && rm -f dist.zip 
cd dist && zip -r ../dist.zip . && cd ../../

# 更新后端：docker cp 进容器 + 清缓存 + restart
docker cp backend/app clawith-backend-1:/app/
docker exec clawith-backend-1 find /app -name "__pycache__" -exec rm -rf {} + 2>/dev/null
docker compose restart backend

# 更新前端：docker cp dist.zip + 解压 + restart
docker cp frontend/dist.zip clawith-frontend-1:/usr/share/nginx/html/dist.zip
docker exec clawith-frontend-1 sh -c "cd /usr/share/nginx/html && unzip -o dist.zip"
docker compose restart frontend
```

## 注意事项
- SSH 到生产环境需要加 `-o PubkeyAuthentication=no` 避免 key 认证失败
- 如果需要测试 Agent 功能，没有特别说明的话，默认使用 **Morty** 这个 Agent 进行测试
- 当你启用浏览器进行验证的时候，登录平台时浏览器会帮你记住密码，所以不用你填写密码，直接点击登录即可。
- 所有更新，不仅要考虑我们自己开发和生产环境，还需要考虑其他已经部署我们平台的用户，在升级到新版本时会碰到的问题，如果需要升级方案等操作，请一并提供出来。