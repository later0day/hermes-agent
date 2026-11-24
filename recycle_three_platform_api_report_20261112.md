# 爱回收 / 拍机堂 / 转转：产品名到问卷与估价接口完整报告

- 报告日期：2026-11-12
- 实时验证输入：`iPhone 18 Pro`
- 实时验证范围：三平台“产品名 → 精确机型 ID → 匿名问卷”全部通过
- 请求纪律：所有本次实时请求均显式使用用户提供的 SOCKS5 代理，DNS 也走代理（`socks5h`）；报告不保存代理、Token、openid、unionid 或商户凭据
- 权威来源：`recycle-questionnaire-sources/SKILL.md`、`paijitang-inquiry-api.md`、`ahs_inquiry_api.md`、实时响应及原始问卷实物

---

## 1. 先明确输入输出边界

### 仅输入产品名可以完成

```text
产品名
  → 搜索候选
  → 精确匹配机型
  → 获取平台机型 ID
  → 拉取完整问卷、题目选项 ID、SKU/步骤信息
```

三平台的搜索和问卷均可匿名完成，不需要用户账号。

### 仅输入产品名不能得到真实成色估价

实际出价还需要用户逐题选择容量、渠道、外观、电池、功能等答案：

- 爱回收：完整 `ppvIds` + 登录 Token + `sensors`，再调用 `create-v4`。
- 拍机堂：完整 `pricePropertyValueIds` + Access-Token，再调用 `by-ppv`。
- 转转：当前资料只验证了帮卖搜索和问卷，未验证成色答案提交/真实出价接口；搜索返回的 `maxPrice` 只是顶配完美成色展示上限，不是用户设备实际估价。

因此，面向最终用户的合理交互是：**先输入产品名，系统展示该平台问卷；用户答完后再提交估价。**

---

## 2. 三平台实时验证结果

输入统一为 `iPhone 18 Pro`，大小写不敏感精确匹配，不取模糊搜索的第一项。

| 平台 | 精确机型及 ID | 搜索候选数 | 问卷结果 | 实时状态 |
|---|---|---:|---|---|
| 爱回收 H5 | 苹果 iPhone 18 Pro，`productId=328679` | 2 | 22 题、2 个 SKU 维度、16 个 SKU 元组、templateType=2 | 搜索+问卷成功 |
| 拍机堂 | 苹果 iPhone 18 Pro，`productId=328679` | 2 | 3 步、35 题、4 个 SKU 维度 | 搜索+问卷成功 |
| 转转帮卖 | iPhone 18 Pro，`modelId=1499188`、`cateId=101`、`brandId=10530` | 2 | 12 题，其中 11 题必答 | 搜索+问卷成功 |

补充结论：

- 本次爱回收与拍机堂独立搜索均得到 `328679`，与既有“同 ID”实测一致；生产流程仍应分别搜索并核对，不硬编码该结论替代搜索。
- 拍机堂匿名搜索和问卷在 `access-token=""`、`chromosome=""` 下本次实测成功；这不代表匿名或空签名可以调用登录后的 `by-ppv` 出价。
- 上述实测没有提交 iPhone 18 Pro 的真实成色答案，因此不包含该机型的实际估价。

---

## 3. 通用产品名处理与匹配规则

1. 对用户输入执行 `trim`，合并连续空白；显示名保留原始大小写，比较时使用 `casefold()`。
2. 搜索后必须精确匹配：
   - 爱回收：`name == "苹果 " + 输入产品名`。
   - 拍机堂：`productName == "苹果 " + 输入产品名`。
   - 转转：`name == 输入产品名`。
3. 精确结果必须是唯一一项：
   - 0 项：返回候选列表让用户选择，不默认取第一项。
   - 多项：返回歧义错误及候选 ID，不自动猜测。
4. 问卷响应必须再次校验机型名/productId，且题目列表非空。
5. 产品 ID、题目和选项可缓存；换机型不得复用另一机型的答案 ID。
6. 爱回收/拍机堂与转转是两套不同的选项 ID 体系，不能跨平台直接复用答案。

---

# 一、转转帮卖

## 4. 转转公共请求配置

```http
User-Agent: Mozilla/5.0 (Linux; Android 14) MicroMessenger/8.0.79 MiniProgramEnv/android
Referer: https://servicewechat.com/
```

- 无登录态。
- 无请求签名。
- 搜索和问卷均为 GET。
- 苹果手机常量实测为 `cateId=101`、`brandId=10530`，但仍以搜索结果为准。

## 5. 搜索：产品名 → modelId/cateId/brandId

### 请求

```http
GET https://app.zhuanzhuan.com/zzopen/zzbmlogic/getWordkeySearchResultNew?keyword=iPhone%2018%20Pro
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `keyword` | 是 | 用户输入产品名，URL 编码 |

### 成功判据与响应

```text
respCode == 0
respData.keyWordResult[] 非空
```

每个候选重点字段：

| 字段 | 用途 |
|---|---|
| `name` | 精确匹配产品名 |
| `modelId` | 问卷请求的机型 ID |
| `cateId` | 问卷请求的品类 ID，手机通常为 101 |
| `brandId` | 问卷请求的品牌 ID，苹果通常为 10530 |
| `brandName` / `cateName` | 品牌/品类回显 |
| `maxPrice` | 搜索展示最高价，不是实际成色估价 |
| `showPrice` / `saleType` / `pic` / `modelType` / `groupName` | 展示和辅助筛选字段 |

本次精确结果：

```json
{
  "name": "iPhone 18 Pro",
  "modelId": 1499188,
  "cateId": 101,
  "brandId": 10530
}
```

## 6. 问卷：getSkuBayesParamPage

### 请求

```http
GET https://app.zhuanzhuan.com/zzopen/zzbmlogic/getSkuBayesParamPage?cateId=101&brandId=10530&modelId=1499188&evalPlanType=B&channel=BM_0040
```

| 参数 | 必填 | 值/说明 |
|---|---|---|
| `cateId` | 是 | 搜索返回值；必须正确，错误时可能 `respCode=0` 但问卷为空 |
| `brandId` | 是 | 搜索返回值；文档实测服务端校验较弱，仍不得省略 |
| `modelId` | 是 | 搜索返回的精确机型 ID |
| `evalPlanType` | 是 | 固定 `B`，帮卖 |
| `channel` | 是 | 固定 `BM_0040` |

### 成功判据与响应

```text
respCode == 0
respData.bayesParamDtoList 非空
respData.modelName 与所选机型一致
```

| 响应字段 | 含义 |
|---|---|
| `respData.modelName` | 机型名回显 |
| `respData.bayesParamDtoList[].itemId` | 题目 ID |
| `respData.bayesParamDtoList[].itemName` | 题目名 |
| `respData.bayesParamDtoList[].required` | `1` 表示必答 |
| `respData.bayesParamDtoList[].options[]` | 选项数组 |
| `options[].id` / `options[].name` | 转转独立选项 ID 和名称 |
| `options[].disabled` | 选项禁用状态等元数据 |

本次实测为 12 题、11 题必答。原始响应见包内 `live/zz_questionnaire_iphone18pro.json`。

## 7. 转转限制与排错

- `respCode=0` 不足以判成功，必须同时要求 `bayesParamDtoList` 非空。
- `getWordkeySearchResultNew` 是当前已验证搜索端点；其他猜测的 model-list 端点曾返回 403，不应盲目穷举。
- 当前报告不包含转转真实成色出价请求；不要把 `maxPrice` 当作用户设备的报价。

---

# 二、爱回收

爱回收需要区分两套网关：

| 用途 | 网关 | Ahs-App-Id | 签名盐 | 登录要求 |
|---|---|---:|---|---|
| 匿名 H5 搜索+问卷 | `https://dubai.aihuishou.com/dubai-gateway` | `10002` | `34694d9d74954784` | 不需要 |
| 小程序真实估价+券 | `https://dubai-mp.aihuishou.com/dubai-gateway` | `10007` | `797d43d445498964` | create/detail/coupon 需要 |

两套环境的 App-Id、盐和响应结构不得混用。

## 8. 爱回收 H5 Sign2

公共请求头：

```http
Ahs-App-Id: 10002
Ahs-Token:
Ahs-Timestamp: <当前毫秒时间戳>
Ahs-Sign2: <按下式计算>
Content-Type: application/json;charset=UTF-8
```

签名段按以下顺序拼接，空段省略，以 `&` 连接后 MD5：

```text
Ahs-App-Id=10002
<裸 querystring>                 # GET 有 query 时；没有 query= 前缀
body=<发送的 compact JSON 原文>   # POST 有 body 时
Ahs-Timestamp=<同一个毫秒时间戳>
Ahs-Once-Token=<token>            # 只有实际使用时才添加
salt=34694d9d74954784
```

约束：

- GET 使用裸 `querystring.stringify(query)`，不使用旧的 `query=<quoted JSON>`。
- POST 签名中的 body 必须与发送字节完全一致，使用 compact JSON。
- URL query 与 POST body 同时存在时，两者按顺序共同签名。
- 使用 Once-Token 时必须同时放入请求头和签名；不用则完全省略。
- 时间戳只读取一次，请求头与签名使用同一值。
- 当前匿名 H5 链只需要 `Ahs-Sign2`，不要添加旧 `Ahs-Sign`。

## 9. 爱回收匿名搜索：产品名 → productId

### 请求

```http
POST https://dubai.aihuishou.com/dubai-gateway/recycle-products/search-v11
Content-Type: application/json;charset=UTF-8
```

```json
{"keyword":"iPhone 18 Pro","cityId":114,"bizTypes":[1],"page":1,"size":10}
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `keyword` | 是 | 用户输入产品名 |
| `cityId` | 是 | 城市 ID；匿名搜索实测使用 114 |
| `bizTypes` | 是 | 必须为 int 数组 `[1]`，不能传字符串数组 |
| `page` | 是 | 页码 |
| `size` | 是 | 每页数量 |

### 成功判据与响应

```text
code == 0
data.products[] 为数组
```

候选项重点字段：`id`（productId）、`name`、`maxPrice`、`imageUrl`。精确匹配 `name == "苹果 iPhone 18 Pro"`。

本次得到：

```json
{"id":328679,"name":"苹果 iPhone 18 Pro"}
```

注意：小程序 `dubai-mp` 的历史响应使用 `data.list[]`，H5 当前实测使用 `data.products[]`，应按网关解析，不要猜成同一字段。

## 10. 爱回收匿名问卷：inquiry-items

### 请求

```http
GET https://dubai.aihuishou.com/dubai-gateway/recycle-products/inquiry-items?cityId=114&productId=328679
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `productId` | 是 | 搜索返回的 `id` |
| `cityId` | 是 | 城市 ID，本次为 114 |
| `quickInquiryItemId` | 否 | 需要指定快估项时添加；匿名 H5 基础问卷可不传 |

### 成功判据与响应

```text
code == 0
data.propertyNames[] 非空
```

| 响应字段 | 含义 |
|---|---|
| `data.templateType` | 模板类型，本次为 2 |
| `data.propertyNames[].id` | 问题 ID（ppnId） |
| `data.propertyNames[].name` | 问题名 |
| `data.propertyNames[].isSkuProperty` | 是否 SKU 维度 |
| `data.propertyNames[].items[]` | 选项数组 |
| `items[].id` / `items[].ppvIds[]` | 选项标识；按当前 H5 实物建立映射并校验 |
| `items[].name` | 选项名称 |
| `data.skus[]` | 合法 SKU ppvId 元组表 |

本次为 22 题、2 个 SKU 问题、16 个合法 SKU 元组。原始响应见包内 `live/ahs_questionnaire_iphone18pro.json`。

H5 与小程序问卷差异：H5 会把“运行情况”等快估题内嵌在 `propertyNames`，所以题数可能比小程序多 1；ppvId 体系一致，但题目覆盖校验必须按当前入口的响应执行。

## 11. 爱回收小程序真实估价链路

### 11.1 登录态配置

通过环境变量注入，不写入源码或报告：

| 环境变量 | 用途 |
|---|---|
| `AHS_TOKEN` | create/detail/coupon 必需登录 Token |
| `AHS_OPENID` / `AHS_UNIONID` | 写入 `sensors.distinctId/unionId` |
| `AHS_PROXY` | 强制 HTTP/SOCKS5 代理 |
| `AHS_DEVICE_ID` | 建议固定 UUID，避免设备画像频繁变化 |

小程序 Sign2 与第 8 节结构相同，但改为：

```text
Ahs-App-Id=10007
...
salt=797d43d445498964
```

必需头还包括 `ahs-app-version: 8.5.5`、`Ahs-Token`，建议携带稳定的 `Ahs-Device-Id` 和每会话一个 `Ahs-Session-Id`。

### 11.2 城市定位

```http
POST https://dubai-mp.aihuishou.com/dubai-gateway/regions/locate-city-v5
```

```json
{"lng":113.65,"lat":34.76}
```

返回 `data.cityId`。该接口实测使用 POST，GET 不作为可靠路径。

### 11.3 产品搜索

```http
POST https://dubai-mp.aihuishou.com/dubai-gateway/recycle-products/search-v11
```

```json
{"keyword":"iPhone 18 Pro","cityId":370,"bizTypes":[1],"page":1,"size":10}
```

从历史小程序响应 `data.list[]` 精确选择 productId；若当前响应 schema 改变，应保存实物并适配，不能默认取第一项。

### 11.4 快估模板

```http
GET https://dubai-mp.aihuishou.com/dubai-gateway/recycle-products/inquiry/{productId}?queryType=1&productId={productId}&cityId=370
```

读取：

- `data.templateType`
- `data.quickInquiry.items[].id`：下一步 `quickInquiryItemId`
- `data.quickInquiry.items[].ppvs`：原始响应是对象数组，取对象 `id` 并入最终 `ppvIds`

### 11.5 完整问卷

```http
GET https://dubai-mp.aihuishou.com/dubai-gateway/recycle-products/inquiry-items?productId={productId}&quickInquiryItemId={qid}&cityId=370&templateType={templateType}
```

校验：

1. 每道必答题都有答案。
2. 每个答案 ID 属于当前 productId 的选项集合。
3. 已选 SKU 维度组合满足 `any(selectedSkuIds <= set(validSkuTuple))`。
4. 快估答案也必须并入 create 的 `ppvIds`。
5. 某些机型存在问卷不可答、但固定在 SKU 元组中的值；详情回显可自动补入这些值，只能接受能由所选 SKU 唯一推导的隐藏固定值。

### 11.6 提交估价 create-v4

```http
POST https://dubai-mp.aihuishou.com/dubai-gateway/inquiry/create-v4
Content-Type: application/json;charset=UTF-8
Ahs-Token: <AHS_TOKEN>
```

```json
{
  "ahsAgent":"AHS_MP",
  "productId":328679,
  "ppvIds":["<快估及完整问卷答案ID，实际请求必须为整数数组>"],
  "phenomenonItemIds":[],
  "isEnvironmentalRecycling":false,
  "autoFulfill":false,
  "cityId":370,
  "templateType":2,
  "sensors":{
    "city_id":370,
    "cityId":370,
    "innerSource":"",
    "sourcePlatform":"miniapp",
    "sourceAppName":"earth",
    "utmAhs":"",
    "distinctId":"<AHS_OPENID>",
    "unionId":"<AHS_UNIONID>",
    "abtest":"",
    "clickId":""
  }
}
```

成功仅返回 `data.inquiryKey`。`sensors` 必填；`ppvIds` 不能由产品名自动推导，必须来自用户实际答案。

### 11.7 价格详情 detail-v2

```http
GET https://dubai-mp.aihuishou.com/dubai-gateway/inquiry/detail-v2?inquiryKey={inquiryKey}
```

重点字段：

- `data.price`：基础估价。
- `data.inquiryValueIds`：答案回显，必须与输入及可推导的隐藏 SKU 固定值严格核对。
- `data.product.id/name`：机型回显。
- `data.chosenPpv.ppvGroups[]`：设备属性、功能、外观分组回显。
- `data.protectPriceTime`、`weekReduction`、`templateType`、`overDue`。

### 11.8 券方案 common-coupon-plan

```http
POST https://dubai-mp.aihuishou.com/dubai-gateway/coupons/common-coupon-plan
```

```json
{"inquiryKey":"<inquiryKey>","cityId":370,"pickupType":5,"bizType":1}
```

券选择：

- 固定回收券：`promotionType=1`、`status=-1`、非百分比、满足 `minPriceLimit`，取最大 `amount`。
- 叠加回收券：`promotionType=4`、`status=-1`、百分比、满足门槛，响应 `amount` 已是实际加价。
- `promotionType=3` 是购买券，不计入回收价。

```text
total = price + 最优固定券 amount
grandTotal = total + 叠加券 amount
```

所有带 `relatedInquiryKey` 的券都应与本次 inquiryKey 一致。

### 11.9 爱回收错误与风控

| code | 含义 | 处理 |
|---:|---|---|
| 1002 | 签名验证失败 | 检查环境 App-Id/盐、毫秒时间戳、GET/POST 段和原始 body |
| 1003 | 用户未登录 | 更新 AHS_TOKEN，不重复发送 |
| 3001 | 图片验证码 | OCR 后用 `create-v4?imgCaptcha=xxxx`；query 与原始 body 共同签名，TTL 约 1 分钟 |
| 3003 | 设备状态异常 | 停止，检查设备/代理画像 |
| 12624004 | 询价项策略不存在 | 重校当前机型必答题、选项和 SKU，不盲目重发 |
| 12624008 | 询价次数超上限 | 停止并等待限额恢复，不能用验证码或重试解决 |

第 9、20 行历史批次已真实验证验证码 query+body 联合 Sign2 重试成功。

---

# 三、拍机堂

## 12. 拍机堂公共请求配置

API 域名：`https://sjapi-mini.aihuishou.com`

```http
referer: https://servicewechat.com/wxfb796f037077edb6/34/page-frame.html
app-id: xcx152403
app-name: xcx
platform: miniapp
ahs-language: zh-CN
content-type: application/json
timestamp: <当前毫秒时间戳>
access-token:
chromosome:
```

- 响应统一为 `{code,resultMessage,data}`，业务成功码是 **200**，不是 0。
- HTTP 200 不代表业务成功，必须检查 body.code。
- 小程序原请求会带抓包 chromosome；配置链历史实测可重放，本次匿名搜索和问卷使用空 chromosome 也成功。
- 出价 `by-ppv` 必须提供有效 Access-Token。

## 13. 拍机堂搜索：产品名 → productId

### 请求

```http
POST https://sjapi-mini.aihuishou.com/sj-inspection-api/xcx-foundation/product
Content-Type: application/json
```

```json
{"keyword":"iPhone 18 Pro"}
```

必须使用 POST JSON；用 GET 调用该业务接口会返回 `20200101 ServletException`。

### 响应

```text
code == 200
data[] 为候选数组
```

候选项字段：`productId`、`productName`、`icon`。精确匹配 `productName == "苹果 iPhone 18 Pro"`，本次得到 `productId=328679`。

## 14. 拍机堂问卷：by-inspection

### 请求

```http
GET https://sjapi-mini.aihuishou.com/sj-inspection-api/price-property/v3/app/xcx/step/product/by-inspection?productId=328679
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `productId` | 是 | 搜索返回值 |

### 成功判据与响应

```text
code == 200
data[] 非空
```

`data[]` 按三个 step 分组：

| step | 内容 |
|---|---|
| step1 | 物品信息/SKU 档位，`isSkuProperty=true` 的属性最关键 |
| step2 | 成色情况 |
| step3 | 功能情况 |

题目结构：

```text
data[].properties[].id
data[].properties[].name
data[].properties[].isSkuProperty
data[].properties[].pricePropertyValues[].id
data[].properties[].pricePropertyValues[].value
data[].properties[].pricePropertyValues[].isPreferred
```

本次 iPhone 18 Pro 实测为 3 步、35 个属性、4 个 SKU 属性。原始响应见包内 `live/pjt_questionnaire_iphone18pro.json`。

非法 productId 返回 `20200100 型号未上架`。

## 15. 拍机堂其他配置接口

| 接口 | 方法/body | 用途 |
|---|---|---|
| `/sj-inspection-api/xcx-foundation/category` | POST `{}` | 品类列表，手机 categoryId=1 |
| `/sj-inspection-api/xcx-foundation/brand` | POST `{"categoryId":1}` | 品牌列表，苹果 brandId=52 |
| `/sj-inspection-api/xcx-foundation/product` | POST `{"brandId":52,"categoryId":1}` | 无关键词时获取机型列表 |
| `/opt-privilege/quote/merchant/mini-program?membershipId=1` | GET | 查询商户询价 VIP 剩余次数 |

产品名搜索和列表模式可并存；面向用户只输入产品名时使用关键词模式。

## 16. 拍机堂真实出价：by-ppv

### 请求

```http
POST https://sjapi-mini.aihuishou.com/supply-api/mini-inquiry-price/price-info/by-ppv
Content-Type: application/json
Access-Token: <PJT_ACCESS_TOKEN>
```

```json
{
  "productId":328679,
  "pricePropertyValueIds":["<用户完整答案ID，实际请求必须为整数数组>"]
}
```

要求：

- 必须带完整有效的 32 位 hex Access-Token；空 token 返回 `400 token为空！`，失效或截断会报 Token 已失效。
- 必须传当前实时问卷要求的完整属性答案。历史文档中的“33 个”是特定机型实例，不能硬编码；本次 iPhone 18 Pro 实物有 35 个属性。
- 缺题返回 `19021020`，`resultMessage` 会列缺失属性 ID，可用于提示用户补答，不应擅自按默认选项补齐。
- `code=1e7` 表示询价次数达到上限。

主要响应字段：

| 字段 | 含义 |
|---|---|
| `guidePrice` | 指导价 |
| `highPrice` | 最高价 |
| `platformReferencePrice` | 平台参考价 |
| `levelName` | 成色等级 |
| `skuId` / `itemCode` | SKU/商品编码 |
| `priceSourceType` | 价格来源类型 |

结果查询路径 `/supplier-api/mini-inquiry-price/price-info/by-mini-inquiry-id` 历史裸调为 404，不作为当前推荐链路。

## 17. 拍机堂登录和商户绑定边界

| 场景 | 端点 | body | 说明 |
|---|---|---|---|
| 老登录端点 | `https://sjapi.aihuishou.com/opt-auth/mini-program/login` | `{"code":"<wx.login code>"}` | 设备小程序内可发 32 位 token；外部服务器历史实测 405，不以外部结果判断设备端下线 |
| v2 登录 | `https://sjapi-mini.aihuishou.com/opt-auth/mini-program-v2/login` | `{"loginCode":"<wx.login code>"}` | 未绑商户时可能只给 wxAccountId/openId，accessToken=null |
| 商户绑定 | `https://sjapi-mini.aihuishou.com/opt-auth/mini-program-v2/bind` | `{"loginCode":"<新鲜code>","phoneCode":"<新鲜手机号授权code>"}` | 成功可自动注册商户并返回 accessToken/mobile/merchantId |

注意：

- `phoneCode` 是微信 `getPhoneNumber` 按钮授权 code，不是短信验证码。
- loginCode/phoneCode 都是一次性短时效凭据，本报告不保存任何真实值。
- 未绑商户的老端点 token 历史实测也能调用 `by-ppv`；bind 主要影响小程序端价格展示和商户能力。
- 登录与 bind 不等于已获得有效询价结果，仍需完整答案和剩余询价次数。

---

## 18. 跨平台机型 ID 锚点

以下是已有缓存和本次实测，可用于对账，不能替代新产品实时搜索。

| 机型 | 爱回收 productId | 拍机堂 productId | 转转 modelId |
|---|---:|---:|---:|
| iPhone 11 | 32290 | 32290 | 2193 |
| iPhone 11 Pro | 32291 | 32291 | 2191 |
| iPhone 11 Pro Max | 32292 | 32292 | 2192 |
| iPhone 12 mini | 36044 | 36044 | 31468 |
| iPhone 12 | 36045 | 36045 | 31458 |
| iPhone 12 Pro | 36046 | 36046 | 31459 |
| iPhone 12 Pro Max | 36047 | 36047 | 31469 |
| iPhone 13 mini | 43512 | 43512 | 58383 |
| iPhone 13 | 43510 | 43510 | 58384 |
| iPhone 13 Pro | 43511 | 43511 | 58386 |
| iPhone 13 Pro Max | 43513 | 43513 | 58387 |
| iPhone 14 | 98525 | 98525 | 95637 |
| iPhone 14 Pro | 98527 | 98527 | 95638 |
| iPhone 14 Pro Max | 98528 | 98528 | 95639 |
| iPhone 17 Pro Max | 225362 | 225362 | 未验证 |
| iPhone 18 Pro（本次实时） | 328679 | 328679 | 1499188 |

转转苹果常量：`cateId=101`、`brandId=10530`。

---

## 19. 推荐的统一接入流程

```text
input productName

normalize(productName)
  ├─ 爱回收搜索 → 唯一精确产品 → productId → H5匿名问卷
  ├─ 拍机堂搜索 → 唯一精确产品 → productId → 三步匿名问卷
  └─ 转转搜索   → 唯一精确产品 → modelId+cateId+brandId → 帮卖匿名问卷

向用户展示所选平台问卷
  ↓
用户提交答案
  ├─ 爱回收：校验题目/选项/SKU → 登录态 create-v4 → detail-v2 → coupon-plan
  ├─ 拍机堂：按实时问卷校验完整属性 → Access-Token by-ppv
  └─ 转转：当前仅返回问卷，真实出价链路标记为未实现/未验证
```

工程约束：

- 搜索无精确结果或出现多个精确结果时，必须要求用户选择，不默认第一项。
- 每次换 productId/modelId 都重新校验题目和选项，不能跨机型复用答案。
- 爱回收 SKU 校验使用合法元组子集检查；拍机堂以实时问卷和缺题错误为准；转转检查必答题及非空问卷。
- 任何验证码、登录失效、限额、参数错误都明确返回，不连续重试制造成功。
- 所有外部请求通过受控代理配置发出，凭据仅通过环境变量或密钥管理注入。

---

## 20. 报告附件与实测边界

报告 ZIP 包含：

- 本报告。
- `live/`：iPhone 18 Pro 三平台搜索和问卷原始响应、验证摘要。
- `samples/`：既有爱回收、拍机堂、转转问卷实物及 ID 映射。
- `implementation/`：爱回收估价脚本和原始接口文档。

未覆盖或不得推断的内容：

1. 本次没有对 iPhone 18 Pro 提交成色答案，因此没有该机型真实出价。
2. 转转真实成色出价接口未在当前资料中验证。
3. 拍机堂登录、bind、by-ppv 的请求结构来自既有设备实测文档，本次只重新验证了匿名搜索和问卷。
4. 爱回收 create/detail/coupon 已有其他机型真实链路及验证码验证，但不应把这些结果冒充为 iPhone 18 Pro 出价。
5. 所有价格、券和 questionnaire 配置都可能随时间变化，生产使用应以实时响应为准。
