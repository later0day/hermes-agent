# DingTalk AI Card IP Whitelist Analysis

Date: 2026-06-20

## Symptom

Recent DingTalk replies arrived as normal webhook messages instead of AI Cards.
The Dashboard Logs page did not show the AI Card failure while managing the
`xcx` profile.

## Findings

The `xcx` gateway did attempt to create AI Cards. The profile gateway log shows
the Card API failing before delivery:

`Forbidden.AccessDenied.IpNotInWhiteList`

The response includes `request ip=2409:...`, which is the outbound address
observed by DingTalk's API. This address is IPv6 because the host/network stack
selected an IPv6 route for `api.dingtalk.com`. If DingTalk's app allowlist only
contains the older IPv4 exit address, Card API calls are rejected.

Normal text still appears because the DingTalk adapter catches AI Card creation
failures and falls back to webhook sending. That fallback is why replies still
arrive, but without the Card UI.

The Dashboard Logs page had a separate profile-scoping bug: `/api/logs` always
read the dashboard process profile. When the UI was managing `profile=xcx`, it
still tailed the default profile log instead of
`~/.hermes/profiles/xcx/logs/gateway.log`.

## Fix

- Made `/api/logs` accept the same `profile` query parameter used by other
  profile-scoped Dashboard endpoints.
- Added `/api/logs` to the frontend profile-scoped endpoint list, so the Logs
  page follows the active Dashboard profile selector.
- Added regression coverage that `/api/logs?profile=xcx` reads the profile log,
  not the default profile log.

## Remaining External Action

Add the current IPv6 outbound address shown in the DingTalk error to the
DingTalk app IP allowlist, or force the host/network to use an allowlisted IPv4
route for DingTalk API calls.

## Follow-up: Native Image Uploads

Later image sending failed on the DingTalk robot media upload endpoint:

```text
DingTalk media upload failed: 60020 访问ip不在白名单之中
request ip=2409:... appKey=...
```

This was a separate outbound path from AI Cards. AI Card create/deliver calls
use the DingTalk OpenAPI SDK, while local image sending first calls
`DingTalkAdapter._upload_robot_media()` and uploads the file with the adapter's
plain `httpx.AsyncClient` to:

```text
https://oapi.dingtalk.com/media/upload
```

That httpx client was initialized only with timeout/connection limits. It did
not get an explicit IPv4-only transport, so even with AI Card traffic confirmed
on the allowlisted IPv4 route, media upload could still leave through the host's
IPv6 route and hit DingTalk error `60020`.

## Media Upload Fix

The DingTalk adapter now builds its shared httpx client through a helper. When
Hermes' global `network.force_ipv4` patch is active, the helper creates:

```text
httpx.AsyncHTTPTransport(local_address="0.0.0.0")
```

That same client is used by session-webhook fallback and robot media upload, so
local image/file upload follows the same IPv4 preference instead of resolving to
IPv6 independently.

Regression coverage:

```text
tests/gateway/test_dingtalk.py: 71 passed
```

Runtime verification after restarting `ai.hermes.gateway-xcx`:

```text
gateway pid: 46341
dashboard status: dingtalk connected
http client kwargs under xcx config: timeout + transport
transport kwargs: local_address="0.0.0.0"
new media whitelist errors after restart: none observed
direct media/upload smoke test: success=True, has_media_id=True
```

## Follow-up: Native Robot Send `chatbotId.notAllow.sendOTO`

After media upload started succeeding, local image delivery still failed at the
next DingTalk step:

```text
DingTalk media uploaded: type=image file=img_...
DingTalk native robot message failed: chatbotId.notAllow.sendOTO
route=batch_send_oto robot_code_source=current_message.robot_code
```

That shows the IPv4/media-upload issue was already past the failing point. The
new failure was the OpenAPI robot send call:

```text
media/upload -> BatchSendOTO(sampleImageMsg/sampleFile)
```

The profile configuration was not missing. The `xcx` profile loaded
`DINGTALK_CLIENT_ID`, `DINGTALK_CLIENT_SECRET`, `DINGTALK_ROBOT_CODE`, and the
root `dingtalk` config values. The loaded `robot_code` matched the configured
app key/chatBotId.

The code issue was field provenance. The incoming DingTalk SDK model has two
separate fields:

```text
robotCode      -> OpenAPI robotCode/chatBotId used for robot sends
chatbotUserId  -> DingTalk account id for the chatbot user
```

Hermes' raw callback backfill collapsed both into `robot_code`:

```text
"robot_code": ("robotCode", "robot_code", "chatbotUserId", "chatbot_user_id")
```

When the SDK mapping missed `robotCode` but the callback contained
`chatbotUserId`, the send path could prefer `current_message.robot_code` and
submit the chatbot user id as `robotCode` to `BatchSendOTO`. DingTalk then
rejected the send as an invalid/disabled chatbot id.

## Native Robot Send Fix

- `chatbotUserId` is now backfilled only as `chatbot_user_id`; it is no longer
  used as `robot_code`.
- `DINGTALK_ROBOT_CODE` is now consumed by gateway config/env override and by
  `DingTalkAdapter` as an explicit robotCode source.
- `dingtalk.robot_code` is bridged from `config.yaml` to platform extra, and
  the dashboard config labels/defaults expose the field as an optional override.
- Native robot sends now prefer explicit metadata, then configured
  `robot_code`, and only then the incoming callback's `robotCode`. The
  configured robot identity is the authoritative local send identity; callback
  fields are a compatibility fallback.
- DM/native attachment sends were temporarily switched to DingTalk's
  `PrivateChatSend` route, using the callback `openConversationId`. That was
  not correct for a DingTalk Stream bot DM. The 2026-06-20 07:09 runtime sample
  changed to:

```text
route=private_chat_send robot_code_source=config.robot_code
Error: resource.not.found code: 400
```

That confirmed the earlier `chatbotId.notAllow.sendOTO` path was gone, but it
also proved the new route was wrong: `PrivateChatSend` could not resolve the
current Stream bot DM as a person-to-person private-chat resource.

The DingTalk SDK's `PrivateChatSendRequest` has a `coolAppCode` field; Hermes
already loaded the profile's `dingtalk.app_code` but did not pass it to the
send request. Native explicit `private_chat_send` sends now include
`coolAppCode` when configured, but this route is not the default for Stream bot
DMs.

Regression coverage:

```text
tests/gateway/test_dingtalk.py
tests/gateway/test_config.py
146 passed
```

Runtime profile verification:

```text
profile: xcx
dingtalk enabled: true
client_id: set
client_secret: set
robot_code: set, same app key/chatBotId
app_code/corp_id/agent_id/card_template_id: set
```

## Follow-up: `resource.not.found` on DM Image Delivery

The 2026-06-20 07:09 and 07:13 failures were not media upload failures:

```text
DingTalk media uploaded: type=image file=img_f6cd53d04cae.jpg
DingTalk native robot message failed: resource.not.found
route=private_chat_send robot_code_source=config.robot_code
```

Runtime probes narrowed this down:

- AI Card sends to the same DM succeeded after `network.force_ipv4` was active.
- `PrivateChatSend(sampleText)` to the same `openConversationId` failed with
  `resource.not.found`.
- `PrivateChatSend(sampleImageMsg)` and `PrivateChatSend(sampleFile)` failed
  with the same `resource.not.found`.
- `BatchSendOTO(sampleText)` to the stored DingTalk staff id `433670` failed
  with `chatbotId.notAllow.sendOTO`.
- A direct probe against the old standard `interactiveCards/send` endpoint
  also failed with `system.error`.
- The current card_1_0 AI Card create+deliver path already worked for text in
  the same DM.
- A direct card_1_0 probe with the same uploaded media id and
  `msgImages=[media_id]` succeeded in the same DM.
- The full `DingTalkAdapter.send_image_file()` path was then smoke-tested
  end-to-end and returned success:

```text
{"send_image_file": true, "message_id": "hermes_img_01908c9cf600", "error": null}
```

Conclusion:

- The current DingTalk Stream DM is an IM_ROBOT/user-to-robot conversation.
- DingTalk's `PrivateChatSend` API is for a different private-chat resource
  shape, so the Stream DM `conversationId` is not found there.
- `BatchSendOTO` is also the wrong default fallback for this deployment's DM
  image delivery because DingTalk rejects the current robot with
  `chatbotId.notAllow.sendOTO`.
- The missing local-fork behavior was not "enable a different DingTalk admin
  switch"; it was the card-based media route. Uploaded images can be delivered
  in DMs by creating the default card_1_0 AI card template with `msgImages`
  populated and delivering it to `dtv1.card//IM_ROBOT.<senderStaffId>`.

Code correction:

- Local image upload still uses `oapi.dingtalk.com/media/upload` and obeys the
  profile's forced IPv4 preference.
- For Stream DMs (`conversationType == "1"`), `send_image_file()` now sends the
  uploaded image through the card_1_0 create+deliver path:
  `DEFAULT_AI_CARD_TEMPLATE_ID` + `sys_full_json_obj.msgImages` +
  `IM_ROBOT.<senderStaffId>`.
- If the card_1_0 image path fails, Hermes falls back to the old native
  robot-image path so group/native behavior remains available.
- Native explicit `private_chat_send` still includes `coolAppCode` when
  configured, but it is not the DM image default.

Regression and live coverage after the correction:

```text
tests/gateway/test_dingtalk.py
tests/gateway/test_config.py
149 passed
py_compile gateway/platforms/dingtalk.py passed
live card_1_0 direct probe: upload=true, card_1_0=true
live adapter send_image_file probe: send_image_file=true
```

## Follow-up: Live Media Matrix

On 2026-06-20 08:11, a live probe sent five outbound payloads to the current
xcx DingTalk DM (`conversationType=1`, staff id `433670`) in this order:
voice, file, video, image, then markdown.

Results:

```text
voice upload:   success
voice send:     failed, robot_1_0 BatchSendOTO(sampleAudio) -> chatbotId.notAllow.sendOTO

file upload:    success
file send:      failed, robot_1_0 BatchSendOTO(sampleFile) -> chatbotId.notAllow.sendOTO

video upload:   success
cover upload:   success
video send:     failed, robot_1_0 BatchSendOTO(sampleVideo) -> chatbotId.notAllow.sendOTO
file fallback:  failed, robot_1_0 BatchSendOTO(sampleFile) -> chatbotId.notAllow.sendOTO

image upload:   success
image send:     success, card_1_0 image route -> hermes_img_798afd697cc6

markdown send:  success, card_1_0 text route -> hermes_b8386676d06e
```

The failed media cases prove upload is not the blocker. All three native media
types get a temporary media id successfully, then fail only at native
`robot_1_0` single-chat delivery. This is the same API-family failure as the
earlier image/file errors, but image now avoids it by using card_1_0.

Alternative Dingmi single-chat APIs were also probed with a small text file:

```text
dingmi/robots/oToMessages/send:
  403 missing Dingmi.Commerce.ReadWrite
dingmi/intelligentRobots/oToMessages/send:
  403 missing Dingmi.CustomerService.ReadWrite
dingmi/officialAccounts/robots/oToMessages/send:
  403 missing Dingmi.OfficialAccountRobot.ReadWrite
```

So Dingmi is not a drop-in replacement for the current Stream bot credentials.

Additional code correction from this probe:

- `DingTalkAdapter.send()` no longer requires a valid `session_webhook` before
  attempting AI Card delivery. Card delivery uses card_1_0 create/deliver and
  does not depend on the callback webhook. The webhook check now happens only
  after the AI Card path fails or is unavailable.
- Regression added:
  `test_send_uses_ai_card_without_session_webhook`.

## Follow-up: Voice/File/Video Forced Through Card

On 2026-06-20 08:16, the same xcx DingTalk DM was probed again, this time
forcing voice, file, and video through `card_1_0` instead of the native
`robot_1_0` message APIs.

Upload results:

```text
voice upload: success, media_id=@lB_PM3mwp02rZLPOUyDPY84XCpC_
file upload:  success, media_id=@lAjPM3lftWBI1LPOP2Uby842bkqt
video upload: success, media_id=@lAbPD2S1hz8xtLPORYhs4s5gZCxK
cover upload: success, media_id=@lADPM3tBvZ4D9LPMtM0BQA
```

Card delivery probes:

```text
voice_msgAudio:  success -> hermes_card_media_voice_msgAudio_26fe7f65
voice_msgAudios: success -> hermes_card_media_voice_msgAudios_6c55ebd1
file_msgFile:    success -> hermes_card_media_file_msgFile_8271684f
file_msgFiles:   success -> hermes_card_media_file_msgFiles_13d58bd3
video_msgVideo:  success -> hermes_card_media_video_msgVideo_5f0358c0
video_msgVideos: success -> hermes_card_media_video_msgVideos_111bee27
```

Interpretation:

- DingTalk accepts and delivers card_1_0 cards carrying these media-shaped
  fields in `sys_full_json_obj`.
- The API success confirms this is a viable delivery path to continue
  investigating for voice/file/video DMs, because it bypasses the failing
  native `BatchSendOTO`/`PrivateChatSend` route.
- The probe can confirm create+deliver success from the API side. It cannot
  inspect the DingTalk client renderer, so the next decision is based on which
  of the delivered cards renders as an actual playable/downloadable attachment
  in the DingTalk UI.
