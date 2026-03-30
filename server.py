"""
Dream11 Points Calculator — Backend API
Run locally:  python server.py
Deploy on:    Render (web service, free tier)
"""

import os, csv, json
from flask import Flask, request, jsonify
from flask_cors import CORS

# ── Google Sheets config ────────────────────────────────────────────────────
SPREADSHEET_ID = "17bRNgcJ8L4LCAlM2eRVpOuF2EYQq6Rqpd-k7-VsIq2o"

# On Render: set env var GOOGLE_CREDENTIALS to the JSON content of your service
# account file, OR place google.json in the same folder.
def _get_creds():
    raw = os.environ.get("GOOGLE_CREDENTIALS")
    if raw:
        import tempfile, json as _j
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
        tmp.write(raw)
        tmp.close()
        return tmp.name
    local = os.path.join(os.path.dirname(__file__), "google.json")
    if os.path.exists(local):
        return local
    raise RuntimeError("No Google credentials found. Set GOOGLE_CREDENTIALS env var or place google.json next to server.py")

# ── T20 points table ────────────────────────────────────────────────────────
T20 = {
    "announced": 4, "run": 1, "boundary_bonus": 4, "six_bonus": 6,
    "25_run_bonus": 4, "50_run_bonus": 8, "75_run_bonus": 12, "100_run_bonus": 16,
    "dismissal_for_duck": -2, "dot_ball": 1, "wicket": 30, "lbw/bowled_bonus": 8,
    "3_wicket_haul": 4, "4_wicket_haul": 8, "5_wicket_haul": 12, "maiden": 12,
    "catch": 8, "3_catch_bonus": 4,
    "runout(DirectHit/Stumping)": 12, "runout(Catcher/Thrower)": 6,
    "min_overs_to_be_bowled_for_economy_points": 2,
    "economy_points": {
        "<5": 6, ">=5 and <=5.99": 4, ">=6 and <=7": 2,
        ">=10 and <=11": -2, ">=11.01 and <=12": -4, ">12": -6,
    },
    "min_balls_to_be_played_for_strikerate_points": 10,
    "strike_rate_points": {
        ">170": 6, ">=150.01 and <=170": 4, ">=130 and <=150": 2,
        ">=60 and <=70": -2, ">=50 and <=59.99": -4, "<50": -6,
    },
}

def _run_bonus(runs):
    if runs >= 100: return T20["100_run_bonus"]
    if runs >= 75:  return T20["75_run_bonus"]
    if runs >= 50:  return T20["50_run_bonus"]
    if runs >= 25:  return T20["25_run_bonus"]
    return 0

def _range_pts(value, rng):
    for cond, pts in rng.items():
        parts = cond.split(" and ")
        try:
            if len(parts) == 1:
                if eval(str(value) + parts[0]):
                    return pts
            else:
                if eval(str(value) + parts[0]) and eval(str(value) + parts[1]):
                    return pts
        except Exception:
            pass
    return 0

def calculate_t20_points(playerstats):
    rows = []
    for player, s in playerstats.items():
        pts = T20["announced"]
        pts += s["Runs Scored"] * T20["run"]
        pts += s["Fours"]       * T20["boundary_bonus"]
        pts += s["Sixes"]       * T20["six_bonus"]
        pts += _run_bonus(s["Runs Scored"])
        if s["Dismissal"] not in ("Did Not Bat", "not out") and s["Runs Scored"] == 0:
            pts += T20["dismissal_for_duck"]
        if s["Balls Faced"] >= T20["min_balls_to_be_played_for_strikerate_points"]:
            pts += _range_pts(s["Strike Rate"], T20["strike_rate_points"])
        pts += s["Wickets"]           * T20["wicket"]
        pts += s["LBW/Bowled Wickets"]* T20["lbw/bowled_bonus"]
        if   s["Wickets"] >= 5: pts += T20["5_wicket_haul"]
        elif s["Wickets"] == 4: pts += T20["4_wicket_haul"]
        elif s["Wickets"] == 3: pts += T20["3_wicket_haul"]
        pts += s["Maidens"]   * T20["maiden"]
        pts += s["Dot Balls"] * T20["dot_ball"]
        if s["Overs Bowled"] >= T20["min_overs_to_be_bowled_for_economy_points"]:
            pts += _range_pts(s["Economy"], T20["economy_points"])
        pts += s["Catches"] * T20["catch"]
        if s["Catches"] >= 3: pts += T20["3_catch_bonus"]
        pts += s["Direct Throw Runout"]                  * T20["runout(DirectHit/Stumping)"]
        pts += s["Runout involving Thrower and Catcher"] * T20["runout(Catcher/Thrower)"]
        rows.append({"player": player, "points": pts})
    return sorted(rows, key=lambda x: x["points"], reverse=True)

# ── Scraper (inline, no import needed) ──────────────────────────────────────
import re, requests as _req

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}
LBW_BOWLED = {"b", "lbw"}

def _empty():
    return {"Runs Scored":0,"Balls Faced":0,"Fours":0,"Sixes":0,"Strike Rate":0.0,
            "Dismissal":"Did Not Bat","Overs Bowled":0.0,"Maidens":0,"Runs Conceded":0,
            "Wickets":0,"LBW/Bowled Wickets":0,"Economy":0.0,"Wides":0,"No Balls":0,
            "Catches":0,"Direct Throw Runout":0,"Runout involving Thrower and Catcher":0,
            "Dot Balls":0}

def _extract_ids(url):
    s = re.search(r'/series/[^/]+-(\d+)/', url)
    m = re.search(r'-(\d+)/full-scorecard', url) or re.search(r'-(\d+)(?:/|$)', url)
    if not s or not m:
        raise ValueError("Could not extract series/match IDs from URL")
    return s.group(1), m.group(1)

def _get_stat(lst, name):
    for s in lst:
        if s.get("name") == name: return s.get("value", 0)
    return 0

def get_playerstats(url):
    series_id, match_id = _extract_ids(url)
    api = f"https://site.api.espn.com/apis/site/v2/sports/cricket/{series_id}/summary?event={match_id}"
    resp = _req.get(api, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    rosters = data.get("rosters")
    if not rosters:
        raise RuntimeError("No roster data. Match may not have started or URL is wrong.")

    ps = {}
    def reg(n):
        if n not in ps: ps[n] = _empty()

    for team in rosters:
        for pe in team.get("roster", []):
            name = pe.get("athlete", {}).get("displayName")
            if not name: continue
            reg(name)
            for pd in pe.get("linescores", []):
                sl = pd.get("statistics", {}).get("categories", [{}])[0].get("stats", [])
                batted = _get_stat(sl, "batted")
                bowled = _get_stat(sl, "inningsBowled")
                if batted:
                    ps[name]["Runs Scored"]  = int(_get_stat(sl, "runs"))
                    ps[name]["Balls Faced"]  = int(_get_stat(sl, "ballsFaced"))
                    ps[name]["Fours"]        = int(_get_stat(sl, "fours"))
                    ps[name]["Sixes"]        = int(_get_stat(sl, "sixes"))
                    ps[name]["Strike Rate"]  = float(_get_stat(sl, "strikeRate") or 0)
                    ps[name]["Dismissal"]    = "not out"
                    for ls in pd.get("linescores", []):
                        bo = ls.get("statistics", {}).get("batting", {})
                        if bo:
                            od = bo.get("outDetails", {})
                            card = od.get("dismissalCard", "").lower()
                            ps[name]["Dismissal"] = od.get("shortText", "not out") or "not out"
                            if card in LBW_BOWLED:
                                bn = od.get("bowler", {}).get("displayName")
                                if bn:
                                    reg(bn); ps[bn]["LBW/Bowled Wickets"] += 1
                            if card not in ("not out","","retired hurt","absent"):
                                flds = od.get("fielders", [])
                                real = [f for f in flds if not f.get("isSubstitute") and f.get("athlete")]
                                if card == "c" and real:
                                    fn = real[0]["athlete"].get("displayName")
                                    if fn: reg(fn); ps[fn]["Catches"] += 1
                                elif card in ("run out","ro"):
                                    if len(real) == 1:
                                        fn = real[0]["athlete"].get("displayName")
                                        if fn: reg(fn); ps[fn]["Direct Throw Runout"] += 1
                                    elif len(real) > 1:
                                        for f in real:
                                            fn = f["athlete"].get("displayName")
                                            if fn: reg(fn); ps[fn]["Runout involving Thrower and Catcher"] += 1
                                elif card == "st" and real:
                                    fn = real[0]["athlete"].get("displayName")
                                    if fn: reg(fn); ps[fn]["Direct Throw Runout"] += 1
                            break
                if bowled:
                    ps[name]["Overs Bowled"]  = float(_get_stat(sl, "overs") or 0)
                    ps[name]["Maidens"]       = int(_get_stat(sl, "maidens"))
                    ps[name]["Runs Conceded"] = int(_get_stat(sl, "conceded"))
                    ps[name]["Wickets"]       = int(_get_stat(sl, "wickets"))
                    ps[name]["Economy"]       = float(_get_stat(sl, "economyRate") or 0)
                    ps[name]["Wides"]         = int(_get_stat(sl, "wides"))
                    ps[name]["No Balls"]      = int(_get_stat(sl, "noballs"))
                    ps[name]["Dot Balls"]     = int(_get_stat(sl, "dots"))
    return ps

# ── Google Sheets helpers ────────────────────────────────────────────────────
def col_letter(n):
    r = ""
    while n > 0:
        n, rem = divmod(n-1, 26)
        r = chr(65+rem) + r
    return r

def push_to_sheets(player_list):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        _get_creds(), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    sheet = sh.get_worksheet(2)
    if sheet is None or sheet.title != "MatchScore":
        sheet = sh.worksheet("MatchScore")
    row4 = sheet.row_values(4)
    target_col = 2
    for i in range(1, len(row4), 2):
        if not row4[i].strip():
            target_col = i + 1
            break
    else:
        last = 1
        for i in range(1, len(row4), 2):
            if row4[i].strip(): last = i
        target_col = last + 3
    pts_col = target_col + 1
    data = [[p["player"], p["points"]] for p in player_list]
    end_row = 4 + len(data) - 1
    sheet.update(values=data, range_name=f"{col_letter(target_col)}4:{col_letter(pts_col)}{end_row}")
    return len(player_list)

def read_sheet(worksheet_index):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        _get_creds(), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.get_worksheet(worksheet_index)
    return {"title": ws.title, "data": ws.get_all_values()}

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # allow Vercel frontend to call this API

@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "Dream11 Points Calculator"})

@app.route("/api/calculate", methods=["POST"])
def calculate():
    body = request.get_json(force=True)
    url  = (body or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        stats       = get_playerstats(url)
        player_list = calculate_t20_points(stats)
        pushed      = push_to_sheets(player_list)
        return jsonify({"success": True, "players": player_list, "pushed": pushed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sheet/<int:index>")
def sheet(index):
    if index not in (0, 1, 2):
        return jsonify({"error": "Sheet index must be 0, 1 or 2"}), 400
    try:
        return jsonify(read_sheet(index))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
