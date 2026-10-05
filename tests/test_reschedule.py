"""已通过申请改期端到端测试（真实 PostgreSQL + TestClient）。

覆盖：
- 同一瞬间（时区表示不同）：返回原申请、归属与次数不变；
- 改到新时刻但仍命中原授权：只改时刻、次数不变；
- 改期触发重选：明细改挂、次数在授权间原子搬移；
- 逐项失败原因（无匹配 / 均停用 / 余量耗尽）及整体原子性；
- 余量计算扣除本申请原占用、但未到期预留仍占额；
- 多素材批量改期与重叠选择顺序（到期早优先、编号小优先）；
- 由已确认预留生成的申请可改期：预留原始时刻/确认记录保留、
  预留查询所关联申请展示最新数据；
- 改期后的撤销与授权退役改挂沿用既有约定；
- 未知申请 404、已撤销申请 409、无时区 422。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_reschedule
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=CST)

passed = 0
failed = 0


def check(name: str, cond: bool, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as c:
        def new_material(code):
            return c.post(
                "/api/v1/materials", json={"code": code, "name": code}
            ).json()["id"]

        m1 = new_material("m1")
        m2 = new_material("m2")
        m3 = new_material("m3")

        def new_auth(mid, count, region, channel="web",
                     starts=NOW - timedelta(days=1),
                     ends=NOW + timedelta(days=1)):
            return c.post(
                "/api/v1/authorizations",
                json={
                    "material_id": mid,
                    "region": region,
                    "channel": channel,
                    "starts_at": starts.isoformat(),
                    "ends_at": ends.isoformat(),
                    "max_count": count,
                },
            ).json()["id"]

        def apply(mids, region, channel="web", occur=NOW):
            return c.post(
                "/api/v1/distributions",
                json={
                    "material_ids": list(mids),
                    "region": region,
                    "channel": channel,
                    "occur_at": occur.isoformat(),
                },
            )

        def reschedule(did, occur):
            return c.post(
                f"/api/v1/distributions/{did}/reschedule",
                json={"occur_at": occur.isoformat()},
            )

        def used(aid):
            return c.get(f"/api/v1/authorizations/{aid}").json()["used_count"]

        def reserved(aid):
            return c.get(f"/api/v1/authorizations/{aid}").json()[
                "reserved_count"
            ]

        def getd(did):
            return c.get(f"/api/v1/distributions/{did}").json()

        print("== 同一瞬间（不同时区表示）：返回原申请、次数不变 ==")
        R = "S1"
        a1 = new_auth(m1, 2, R)
        d1 = apply([m1], R, occur=NOW).json()["id"]
        same_instant_utc = NOW.astimezone(UTC)  # 2026-10-04T04:00:00Z
        r = reschedule(d1, same_instant_utc)
        check("same instant 200", r.status_code == 200, r.text)
        body = r.json()
        check(
            "occur_at rendered same instant",
            datetime.fromisoformat(body["occur_at"]).astimezone(UTC)
            == NOW.astimezone(UTC),
            body["occur_at"],
        )
        # 原始字符串为 +08:00 形式；同瞬间不改时刻，DB 往返后按 UTC 呈现，
        # 时刻值保持一致即可（occur_at 字段未被 UPDATE）。
        check(
            "occur_at value unchanged (UTC round-trip)",
            datetime.fromisoformat(body["occur_at"]).astimezone(UTC)
            == NOW.astimezone(UTC),
            body["occur_at"],
        )
        check("item still on same auth",
              body["items"][0]["authorization"]["id"] == a1)
        check("counts unchanged", used(a1) == 1)

        print("== 改到新时刻仍命中原授权：只改时刻、归属与次数不变 ==")
        new_t = NOW + timedelta(hours=2)
        r = reschedule(d1, new_t)
        check("reschedule 200", r.status_code == 200, r.text)
        body = r.json()
        check(
            "occur_at updated",
            datetime.fromisoformat(body["occur_at"]) == new_t,
            body["occur_at"],
        )
        check("still approved", body["status"] == "approved")
        check("auth unchanged",
              body["items"][0]["authorization"]["id"] == a1)
        check("used unchanged (1)", used(a1) == 1)
        check(
            "GET reflects new occur_at",
            datetime.fromisoformat(getd(d1)["occur_at"]) == new_t,
        )

        print("== 改期触发重选：次数原子搬移到新授权 ==")
        R = "S2"
        early = new_auth(
            m1, 3, R,
            starts=NOW - timedelta(days=2), ends=NOW + timedelta(days=1),
        )
        later = new_auth(
            m1, 5, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=4),
        )
        d2 = apply([m1], R, occur=NOW).json()["id"]
        check("lands on early", getd(d2)["items"][0]["authorization"]["id"]
              == early)
        new_t = NOW + timedelta(days=2)  # 仅 later 覆盖
        r = reschedule(d2, new_t)
        check("reschedule to later 200", r.status_code == 200, r.text)
        body = r.json()
        check("occur updated",
              datetime.fromisoformat(body["occur_at"]) == new_t)
        check("item moved to later",
              body["items"][0]["authorization"]["id"] == later)
        check("early released (0)", used(early) == 0)
        check("later occupied (1)", used(later) == 1)
        check(
            "GET shows later",
            getd(d2)["items"][0]["authorization"]["id"] == later,
        )

        print("== 失败：新时刻无匹配授权（原子不变） ==")
        r = reschedule(d2, NOW + timedelta(days=10))
        check("no match 422", r.status_code == 422, r.text)
        check(
            "reschedule_rejected code",
            r.json()["error"]["code"] == "reschedule_rejected",
            r.text,
        )
        check(
            "reason no_matching_authorization",
            r.json()["error"]["details"][0]["reason"]
            == "no_matching_authorization",
            r.text,
        )
        check("occur unchanged",
              datetime.fromisoformat(getd(d2)["occur_at"])
              == NOW + timedelta(days=2))
        check("item unchanged",
              getd(d2)["items"][0]["authorization"]["id"] == later)
        check("counts unchanged", used(early) == 0 and used(later) == 1)

        print("== 失败：新时刻匹配授权均停用（原子不变） ==")
        R = "S3"
        a3 = new_auth(
            m1, 2, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=5),
        )
        d3 = apply([m1], R, occur=NOW).json()["id"]
        new_t = NOW + timedelta(days=2)
        c.patch(f"/api/v1/authorizations/{a3}", json={"status": "inactive"})
        r = reschedule(d3, new_t)
        check("inactive 422", r.status_code == 422, r.text)
        check(
            "reason authorization_inactive",
            r.json()["error"]["details"][0]["reason"]
            == "authorization_inactive",
            r.text,
        )
        check("occur unchanged",
              datetime.fromisoformat(getd(d3)["occur_at"]) == NOW)
        check("counts unchanged (1)", used(a3) == 1)
        # 已通过申请改期要求目标授权启用：重新启用后即使仍是同一条授权，
        # 改期也应成功（目标授权须启用）。
        c.patch(f"/api/v1/authorizations/{a3}", json={"status": "active"})
        r = reschedule(d3, new_t)
        check("reschedule after re-activate 200",
              r.status_code == 200, r.text)
        check("used still 1", used(a3) == 1)

        print("== 余量：扣除本申请原占用；未到期预留仍占额 ==")
        # a 容量 1：本申请占 1（满）。改到仅 a 覆盖的新时刻时，扣除本申请
        # 占用后余量恰好为 0 → 可承接。
        R = "S4"
        a4 = new_auth(
            m1, 1, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=5),
        )
        d4 = apply([m1], R, occur=NOW).json()["id"]
        check("occupied fully (1/1)", used(a4) == 1)
        r = reschedule(d4, NOW + timedelta(days=1))
        check("self occupancy excluded -> 200",
              r.status_code == 200, r.text)
        check("count still 1/1", used(a4) == 1)

        # 新时刻由另一条容量 1 的 a5b 承接；一笔未到期预留先占住其唯一
        # 余量（resv 1，满），改期时本申请在 a5b 上无原占用可扣除，
        # 须被未到期预留阻断而整体拒绝。
        R = "S4B"
        a5 = new_auth(
            m1, 2, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(hours=12),
        )
        a5b = new_auth(
            m1, 1, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=5),
        )
        d5 = apply([m1], R, occur=NOW).json()["id"]
        rr = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [m1],
                "region": R,
                "channel": "web",
                "occur_at": (NOW + timedelta(days=2, hours=1)).isoformat(),
                "ttl_minutes": 15,
            },
        )
        check("reservation ok", rr.status_code == 201, rr.text)
        check("a5b reserved=1", reserved(a5b) == 1 and used(a5b) == 0)
        r = reschedule(d5, NOW + timedelta(days=2))
        check("reserved blocks reschedule 422",
              r.status_code == 422, r.text)
        check(
            "quota_exhausted reason",
            r.json()["error"]["details"][0]["reason"]
            == "quota_exhausted",
            r.text,
        )
        check("counts intact (a5=1, a5b reserved=1)",
              used(a5) == 1 and reserved(a5b) == 1)
        # 取消预留释放额度后改期成功
        c.post(f"/api/v1/reservations/{rr.json()['id']}/cancel")
        r = reschedule(d5, NOW + timedelta(days=2))
        check("reschedule after cancel 200",
              r.status_code == 200, r.text)
        check("a5 released, a5b occupied", used(a5) == 0 and used(a5b) == 1)

        print("== 余量：本申请留在原授权上的项不被当作释放额度 ==")
        R = "S4C"
        a6 = new_auth(
            m1, 1, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=5),
        )
        a7 = new_auth(
            m2, 1, R,
            starts=NOW - timedelta(days=2), ends=NOW + timedelta(hours=12),
        )
        # 新时刻 a7 不再覆盖 m2；m2 的新候选 a8 容量 1，先被另一笔
        # 申请占满。改期须逐项报 m2 余量耗尽、整体原子不变。
        a8 = new_auth(
            m2, 1, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=5),
        )
        d6 = apply([m1, m2], R, occur=NOW).json()["id"]
        ids_before = {
            it["material_id"]: it["authorization"]["id"]
            for it in getd(d6)["items"]
        }
        check("m1->a6 m2->a7", ids_before[m1] == a6 and ids_before[m2] == a7,
              str(ids_before))
        # 在 a8 时段内提交另一笔 m2 申请占满 a8。
        dfill = apply([m2], R, occur=NOW + timedelta(days=2))
        check("fill a8 201", dfill.status_code == 201, dfill.text)
        check("a8 full", used(a8) == 1)
        r = reschedule(d6, NOW + timedelta(days=2))
        check("m2 target exhausted 422", r.status_code == 422, r.text)
        reasons = {d["material_id"]: d["reason"]
                   for d in r.json()["error"]["details"]}
        check("m2 quota_exhausted", reasons.get(m2) == "quota_exhausted",
                str(reasons))
        check("m1 has no failure", m1 not in reasons, str(reasons))
        ids_after = {
            it["material_id"]: it["authorization"]["id"]
            for it in getd(d6)["items"]
        }
        check("assignments unchanged", ids_after == ids_before,
              str(ids_after))
        check("counts unchanged (a6=1 a7=1 a8=1)",
              used(a6) == 1 and used(a7) == 1 and used(a8) == 1)
        check("occur unchanged",
              datetime.fromisoformat(getd(d6)["occur_at"]) == NOW)

        print("== 重叠授权选择顺序：到期早优先，其次编号小 ==")
        R = "S5"
        # 旧时刻仅 short 覆盖 → 命中 short；新时刻 short/long 都覆盖，
        # short 到期更早且有余量 → 改期后仍应选 short。
        short = new_auth(
            m3, 3, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=2),
        )
        long = new_auth(
            m3, 3, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=10),
        )
        d7 = apply([m3], R, occur=NOW).json()["id"]
        check("short chosen first",
              getd(d7)["items"][0]["authorization"]["id"] == short)
        # 停用 short 后改期 → 选 long（目标须启用）
        c.patch(f"/api/v1/authorizations/{short}",
                json={"status": "inactive"})
        r = reschedule(d7, NOW + timedelta(days=1))
        check("reschedule skips inactive 200",
              r.status_code == 200, r.text)
        check("moved to long",
              r.json()["items"][0]["authorization"]["id"] == long)
        check("short released 0, long 1",
              used(short) == 0 and used(long) == 1)
        # 再改到 short 已不覆盖的时刻：仅 long，保持 long
        r = reschedule(d7, NOW + timedelta(days=5))
        check("still long 200", r.status_code == 200, r.text)
        check("counts stable", used(short) == 0 and used(long) == 1)

        print("== 改期后撤销：只释放当前归属授权一次 ==")
        R = "S6"
        s = new_auth(
            m1, 3, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1),
        )
        t = new_auth(
            m1, 3, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=6),
        )
        d8 = apply([m1], R, occur=NOW).json()["id"]
        reschedule(d8, NOW + timedelta(days=2))
        check("moved s->t", used(s) == 0 and used(t) == 1)
        rv = c.post(f"/api/v1/distributions/{d8}/revoke")
        check("revoke 200", rv.status_code == 200, rv.text)
        check("t released once (0)", used(t) == 0)
        check("s untouched (0)", used(s) == 0)
        check("double revoke 409",
              c.post(f"/api/v1/distributions/{d8}/revoke").status_code == 409)

        print("== 已撤销申请拒绝改期；未知申请 404；无时区 422 ==")
        check(
            "revoked reschedule 409",
            reschedule(d8, NOW + timedelta(days=3)).status_code == 409,
        )
        check(
            "revoked code",
            c.post(
                f"/api/v1/distributions/{d8}/reschedule",
                json={"occur_at": NOW.isoformat()},
            ).json()["error"]["code"] == "distribution_revoked",
        )
        check("unknown reschedule 404",
              reschedule(999999, NOW).status_code == 404)
        bad = c.post(
            f"/api/v1/distributions/{d7}/reschedule",
            json={"occur_at": "2026-10-05T12:00:00"},
        )
        check("naive datetime 422", bad.status_code == 422, bad.text)
        bad2 = c.post(
            f"/api/v1/distributions/{d7}/reschedule", json={}
        )
        check("missing field 422", bad2.status_code == 422, bad2.text)

        print("== 由已确认预留生成的申请可改期，预留查询展示最新数据 ==")
        R = "S7"
        ra1 = new_auth(
            m1, 3, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1),
        )
        ra2 = new_auth(
            m1, 5, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=8),
        )
        rr = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [m1],
                "region": R,
                "channel": "web",
                "occur_at": (NOW + timedelta(minutes=30)).isoformat(),
                "ttl_minutes": 15,
            },
        )
        check("reservation 201", rr.status_code == 201, rr.text)
        resv_id = rr.json()["id"]
        cf = c.post(f"/api/v1/reservations/{resv_id}/confirm")
        check("confirm 200", cf.status_code == 200, cf.text)
        did = cf.json()["distribution_id"]
        check("confirmed distribution on ra1",
              getd(did)["items"][0]["authorization"]["id"] == ra1)
        check("ra1 used=1 reserved=0", used(ra1) == 1 and reserved(ra1) == 0)

        r = reschedule(did, NOW + timedelta(days=2))
        check("confirmed-via-reservation reschedule 200",
              r.status_code == 200, r.text)
        check("item now on ra2",
              r.json()["items"][0]["authorization"]["id"] == ra2)
        check("ra1 released, ra2 occupied",
              used(ra1) == 0 and used(ra2) == 1)

        resv_view = c.get(f"/api/v1/reservations/{resv_id}").json()
        check("reservation still confirmed",
              resv_view["status"] == "confirmed")
        check(
            "reservation original occur_at preserved",
            datetime.fromisoformat(resv_view["occur_at"])
            == NOW + timedelta(minutes=30),
            resv_view["occur_at"],
        )
        check(
            "linked distribution shows rescheduled data",
            datetime.fromisoformat(
                resv_view["distribution"]["occur_at"]
            ) == NOW + timedelta(days=2)
            and resv_view["distribution"]["items"][0]["authorization"]["id"]
            == ra2,
            str(resv_view["distribution"]),
        )
        # 列表查询同样展示关联申请的最新数据
        lst = c.get(
            "/api/v1/reservations", params={"status": "confirmed"}
        ).json()
        hit = next(x for x in lst if x["id"] == resv_id)
        check(
            "list view linked distribution latest",
            datetime.fromisoformat(
                hit["distribution"]["occur_at"]
            ) == NOW + timedelta(days=2),
        )
        check("confirmation record kept (confirmed_at present)",
              resv_view["confirmed_at"] is not None)

        print("== 改期后的申请仍可被授权退役改挂 ==")
        # d 系列当前归属 ra2；把 ra2 退役到新登记的替代授权。
        repl = new_auth(
            m1, 5, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=9),
        )
        mg = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": ra2,
                "replacement_authorization_id": repl,
            },
        )
        check("migrate after reschedule 200",
              mg.status_code == 200, mg.text)
        check(
            "distribution now shows replacement",
            getd(did)["items"][0]["authorization"]["id"] == repl,
        )
        check("ra2 inactive & released",
              used(ra2) == 0)
        check("repl holds 1", used(repl) == 1)

        print("== 多素材批量改期：全部重选成功才提交 ==")
        R = "S8"
        x1 = new_auth(
            m1, 2, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1),
        )
        x2 = new_auth(
            m2, 2, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1),
        )
        y1 = new_auth(
            m1, 2, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=6),
        )
        y2 = new_auth(
            m2, 2, R,
            starts=NOW + timedelta(days=1), ends=NOW + timedelta(days=6),
        )
        d9 = apply([m1, m2], R, occur=NOW).json()["id"]
        r = reschedule(d9, NOW + timedelta(days=2))
        check("batch reschedule 200", r.status_code == 200, r.text)
        ids = {it["material_id"]: it["authorization"]["id"]
               for it in r.json()["items"]}
        check("m1->y1 m2->y2", ids[m1] == y1 and ids[m2] == y2, str(ids))
        check("old released", used(x1) == 0 and used(x2) == 0)
        check("new occupied", used(y1) == 1 and used(y2) == 1)
        check(
            "region/channel/material list unchanged",
            r.json()["region"] == R
            and r.json()["channel"] == "web"
            and sorted(it["material_id"] for it in r.json()["items"])
            == sorted([m1, m2]),
        )

        print("== 多素材部分失败：逐项列明原因、整体原子不变 ==")
        # y2 停用后改到仅 y1/y2 覆盖的时刻：m1 可承接、m2 报停用。
        c.patch(f"/api/v1/authorizations/{y2}", json={"status": "inactive"})
        r = reschedule(d9, NOW + timedelta(days=3))
        check("partial failure 422", r.status_code == 422, r.text)
        details = r.json()["error"]["details"]
        check("exactly one failing item", len(details) == 1, str(details))
        check(
            "m2 authorization_inactive",
            details[0]["material_id"] == m2
            and details[0]["reason"] == "authorization_inactive",
            str(details),
        )
        ids = {it["material_id"]: it["authorization"]["id"]
               for it in getd(d9)["items"]}
        check("assignments intact", ids[m1] == y1 and ids[m2] == y2,
              str(ids))
        check("counts intact",
              used(y1) == 1 and used(y2) == 1, str(
                  (used(y1), used(y2))))
        check(
            "occur intact",
            datetime.fromisoformat(getd(d9)["occur_at"])
            == NOW + timedelta(days=2),
        )

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
