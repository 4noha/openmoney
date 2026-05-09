"""期首開始仕訳: 前年の期末 BS から当年 1/1 付の開始仕訳を生成。

複式簿記の慣行に従い、年度の最初に「前期繰越」として:
  借方 (各資産科目)   ¥X / 貸方 3001 元入金   ¥X
  借方 3001 元入金     ¥Y / 貸方 (各負債科目) ¥Y
  借方 3001 元入金     ¥Z / 貸方 3003 事業主借 ¥Z (前期 closing 後に残る場合)
  借方 3002 事業主貸   ¥W / 貸方 3001 元入金   ¥W (同上)

差し引きで 3001 元入金 の借方計-貸方計 = 純資産 (資産-負債) になり、
当期仕訳と合算した期末 BS が貸借平衡する。

source='opening' で管理。rebuild_opening(year) は冪等。

前提:
- 前年に rebuild_closing(year-1) が実行済み (事業主借/貸 → 元入金 集約)
- 前年がスナップショット済みなら、ロック中の数値を信頼

注意点:
- 当期純利益分は仕訳化されないため、3001 期末残には前期純利益が反映されない。
  申告書 B 集計や BS 表示では equity_total_with_income で当期純利益を加算する。
- aggregate.compute_bs は source='opening' 仕訳がある年度では opening_balances を
  使わない (二重計上回避)。
"""
from __future__ import annotations

import sqlite3

from src.aoiro.aggregate import compute_bs


_MOTOIREKIN = "3001"


def rebuild_opening(con: sqlite3.Connection, year: int) -> dict:
    """指定年度の opening 仕訳を再生成。

    前年の compute_bs を取得し、各資産/負債/資本科目の期末残を 1/1 付で
    繰越仕訳として書き込む。3001 を相手勘定にすることで、3001 の差額が
    自動的に純資産 (資産-負債) になる。
    """
    con.execute(
        "DELETE FROM aoiro_journal_entries WHERE fiscal_year=? AND source='opening'",
        (year,),
    )
    prev_year = year - 1
    try:
        prev_bs = compute_bs(con, prev_year)
    except Exception as e:
        return {"year": year, "inserted": 0, "skipped": True, "reason": f"prev BS unavailable: {e}"}

    date = f"{year:04d}/01/01"
    inserted = 0

    def _insert(debit_acc: str, credit_acc: str, amount: int, label: str) -> None:
        nonlocal inserted
        if amount <= 0:
            return
        con.execute(
            "INSERT INTO aoiro_journal_entries "
            "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
            " description, source) VALUES (?,?,?,?,?,?,?,'opening')",
            (year, date, debit_acc, amount, credit_acc, amount, label[:300]),
        )
        inserted += 1

    has_prev_bs = bool(
        prev_bs.get("asset_lines") or prev_bs.get("liability_lines") or prev_bs.get("equity_lines")
    )

    if not has_prev_bs:
        # 前年 BS が空 (前年が legacy import のみ等) の場合、当年の
        # aoiro_opening_balances テーブルを期首として使うフォールバック。
        # ユーザが前年末残高を手動入力するシナリオに対応。
        accs = {r["code"]: dict(r) for r in con.execute(
            "SELECT code, name, type FROM aoiro_accounts"
        ).fetchall()}
        rows = con.execute(
            "SELECT account, debit_balance, credit_balance "
            "FROM aoiro_opening_balances WHERE fiscal_year=?", (year,)
        ).fetchall()
        if not rows:
            return {"year": year, "inserted": 0, "skipped": True,
                    "reason": "prev BS empty and no opening_balances"}
        for r in rows:
            acc_code = r["account"]
            acc = accs.get(acc_code, {"name": acc_code, "type": ""})
            db = int(r["debit_balance"] or 0)
            cb = int(r["credit_balance"] or 0)
            net = db - cb
            if net == 0:
                continue
            if net > 0:
                # 借方残 (資産系) → 借方=acc / 貸方=元入金
                _insert(acc_code, _MOTOIREKIN, net,
                        f"[期首繰越] {acc['name']} 借方残 ¥{net:,}")
            else:
                # 貸方残 (負債/資本系) → 借方=元入金 / 貸方=acc
                _insert(_MOTOIREKIN, acc_code, -net,
                        f"[期首繰越] {acc['name']} 貸方残 ¥{-net:,}")
        con.commit()
        return {"year": year, "inserted": inserted, "source": "opening_balances"}

    # 各資産: 借方=資産 / 貸方=元入金
    for line in prev_bs["asset_lines"]:
        amt = int(line["ending"] or 0)
        if amt > 0:
            _insert(line["code"], _MOTOIREKIN, amt,
                    f"[期首繰越] {line['name']} 前期末残 ¥{amt:,}")
        elif amt < 0:
            # マイナス資産 (例: 当座借越) → 借方=元入金 / 貸方=資産
            _insert(_MOTOIREKIN, line["code"], -amt,
                    f"[期首繰越] {line['name']} 前期末残 ¥{amt:,} (借方残)")

    # 各負債: 借方=元入金 / 貸方=負債
    for line in prev_bs["liability_lines"]:
        amt = int(line["ending"] or 0)
        if amt > 0:
            _insert(_MOTOIREKIN, line["code"], amt,
                    f"[期首繰越] {line['name']} 前期末残 ¥{amt:,}")
        elif amt < 0:
            # 借方残の負債 (本来あり得ないが保護のため対称に処理)
            _insert(line["code"], _MOTOIREKIN, -amt,
                    f"[期首繰越] {line['name']} 前期末残 ¥{amt:,} (借方残)")

    # equity 系: 元入金 (3001) は相手勘定として自動的に積み上がるのでスキップ。
    # 3002 事業主貸 / 3003 事業主借 等が前年 closing で 0 集約されていなかった場合のみ繰越。
    for line in prev_bs["equity_lines"]:
        if line["code"] == _MOTOIREKIN:
            continue
        amt = int(line["ending"] or 0)
        if amt > 0:
            # 貸方残 (例: 3003 事業主借) → 借方=元入金 / 貸方=その科目
            _insert(_MOTOIREKIN, line["code"], amt,
                    f"[期首繰越] {line['name']} 前期末残 ¥{amt:,}")
        elif amt < 0:
            # 借方残 (例: 3002 事業主貸) → 借方=その科目 / 貸方=元入金
            _insert(line["code"], _MOTOIREKIN, -amt,
                    f"[期首繰越] {line['name']} 前期末残 ¥{amt:,} (借方残)")

    con.commit()
    return {
        "year": year,
        "inserted": inserted,
        "prev_asset_total": prev_bs["asset_total"],
        "prev_liability_total": prev_bs["liability_total"],
        "prev_equity_total_with_income": prev_bs["equity_total_with_income"],
    }


def has_opening_entries(con: sqlite3.Connection, year: int) -> bool:
    """指定年度に source='opening' 仕訳があるか。"""
    return con.execute(
        "SELECT 1 FROM aoiro_journal_entries WHERE fiscal_year=? AND source='opening' LIMIT 1",
        (year,),
    ).fetchone() is not None


def estimate_opening_balances(con: sqlite3.Connection, year: int) -> dict:
    """transactions の balance + rebuild 後の差額から期首残高を推定して
    aoiro_opening_balances に upsert。

    アルゴリズム:
      STEP A. 1002 普通預金 期首
        - 当年 1/1 以降の MUFG 最初取引の (balance + debit - credit) = 前日終わり残高
      STEP B. 2010 未払金 期首
        - 1002 だけ登録した状態で rebuild_year → BS の 2010 期末が借方残なら
          その絶対値が期首未払金 (前期繰越分)
        - 借方残でなければ期首 0 (貸方残なら登録しない or 既にあるならクリア)

    最後に rebuild_year を呼んで opening 仕訳を反映させる。
    """
    from src.aoiro.aggregate import compute_bs, upsert_opening_balance
    from src.aoiro.engine import rebuild_year

    yyyy = f"{year:04d}"
    notes: list[str] = []

    # STEP A: 1002 普通預金 期首
    r = con.execute(
        "SELECT date, balance, debit, credit FROM transactions "
        "WHERE bank='MUFG' AND substr(date,1,4)=? AND balance IS NOT NULL "
        "ORDER BY date, id LIMIT 1",
        (yyyy,),
    ).fetchone()
    estimated_1002 = 0
    if r:
        estimated_1002 = int(r["balance"]) + int(r["debit"] or 0) - int(r["credit"] or 0)
        notes.append(
            f"1002 普通預金 期首 ¥{estimated_1002:,} "
            f"(MUFG {r['date']} balance ¥{r['balance']:,} を逆算)"
        )
        if estimated_1002 >= 0:
            upsert_opening_balance(con, fiscal_year=year, account="1002",
                                    debit_balance=estimated_1002, credit_balance=0)
        else:
            upsert_opening_balance(con, fiscal_year=year, account="1002",
                                    debit_balance=0, credit_balance=-estimated_1002)
    else:
        notes.append(f"1002 普通預金: MUFG transactions 無し ({yyyy})")

    # まず 2010 を空にしてから rebuild (差額検出のため)
    con.execute(
        "DELETE FROM aoiro_opening_balances WHERE fiscal_year=? AND account='2010'",
        (year,),
    )
    con.commit()

    # STEP B: 1002 だけ登録した状態で一旦 rebuild → 2010 期末を見る
    rebuild_year(con, year)
    bs = compute_bs(con, year)
    # 2010 が liability_lines にあれば貸方残、asset_lines にあれば借方残として表示される
    ending_2010 = 0
    for line in bs.get("liability_lines", []):
        if line["code"] == "2010":
            ending_2010 = int(line["ending"] or 0)
            break
    else:
        for line in bs.get("asset_lines", []):
            if line["code"] == "2010":
                ending_2010 = -int(line["ending"] or 0)  # 借方残ならマイナスとして扱う
                break

    estimated_2010 = 0
    if ending_2010 < 0:
        # 借方残 = 期首未払金が不足 → その絶対値を期首として登録
        estimated_2010 = -ending_2010
        upsert_opening_balance(con, fiscal_year=year, account="2010",
                                debit_balance=0, credit_balance=estimated_2010)
        notes.append(f"2010 未払金 期首 ¥{estimated_2010:,} (rebuild 後の借方残から推定)")
        # 期首を入れた状態で再度 rebuild → 2010 期末 0 になるはず
        rebuild_year(con, year)
    elif ending_2010 > 0:
        notes.append(
            f"2010 未払金 期末 ¥{ending_2010:,} は貸方残 (= 当期に新規発生分)。期首は 0 とする"
        )
    else:
        notes.append("2010 未払金 期末は 0 (期首も 0)")

    # 検証用に最終 BS を返す
    final_bs = compute_bs(con, year)
    return {
        "year": year,
        "estimated_1002_debit": estimated_1002 if estimated_1002 >= 0 else 0,
        "estimated_1002_credit": -estimated_1002 if estimated_1002 < 0 else 0,
        "estimated_2010_credit": estimated_2010,
        "notes": notes,
        "balance_check": final_bs.get("balance_check"),
    }
