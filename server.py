from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import sqlite3
from datetime import datetime
import threading
import time
import re

app = Flask(__name__)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

BASE_URL = "https://cp2.deltatelecom.net.pk"
DEFAULT_USER = "saadakram246"
DEFAULT_PASS = "14Aug@1997"

def init_db():
    conn = sqlite3.connect('telemetry.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_states (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    last_status TEXT,
                    last_seen TEXT,
                    last_usage_gb REAL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS session_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    username TEXT,
                    status TEXT,
                    event_time TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_snapshots (
                    user_id TEXT,
                    record_date TEXT,
                    cumulative_gb REAL,
                    daily_consumed_gb REAL,
                    PRIMARY KEY(user_id, record_date)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS interval_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    log_time TEXT,
                    delta_mb REAL
                )''')
    conn.commit()
    conn.close()

init_db()

def parse_gb(usage_str):
    try:
        match = re.search(r'([\d\.]+)\s*GB', usage_str, re.I)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return 0.0

def get_authenticated_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    })
    login_url = f"{BASE_URL}/login"
    res_get = s.get(login_url, timeout=25)
    soup = BeautifulSoup(res_get.text, 'html.parser')

    form_inputs = {}
    form = soup.find('form')
    form_action = login_url
    if form:
        action = form.get('action')
        if action:
            form_action = action if action.startswith('http') else f"{BASE_URL.rstrip('/')}/{action.lstrip('/')}"
        for inp in form.find_all('input'):
            if inp.get('name'):
                form_inputs[inp.get('name')] = inp.get('value', '')

    payload = dict(form_inputs)
    payload['username'] = DEFAULT_USER
    payload['password'] = DEFAULT_PASS
    s.post(form_action, data=payload, headers={"Referer": login_url}, timeout=25)
    return s

def fetch_portal_data():
    session = get_authenticated_session()
    page_url = f"{BASE_URL}/user/all"
    res_page = session.get(page_url, timeout=25)
    csrf_token = session.cookies.get('ci_csrf_token', '')
    if not csrf_token:
        soup = BeautifulSoup(res_page.text, 'html.parser')
        meta_hash = soup.find('meta', attrs={'name': 'csrf_token_hash'})
        if meta_hash:
            csrf_token = meta_hash.get('content', '')

    ajax_endpoint = f"{BASE_URL}/admin_portal/user/user/getServerSideUsers"
    payload = {
        "draw": "1",
        "start": "0",
        "length": "500",
        "search[value]": "",
        "search[regex]": "false"
    }
    if csrf_token:
        payload['ci_csrf_token'] = csrf_token

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page_url,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    }

    dt_res = session.post(ajax_endpoint, data=payload, headers=headers, timeout=30)
    data_json = dt_res.json()
    records = data_json.get('data', [])

    clean_subscribers = []
    for row in records:
        if isinstance(row, list):
            clean_subscribers.append([BeautifulSoup(str(col), 'html.parser').get_text(strip=True) for col in row])
        else:
            clean_subscribers.append(row)
    return clean_subscribers

def background_poller():
    last_ten_min_check = 0
    while True:
        try:
            records = fetch_portal_data()
            conn = sqlite3.connect('telemetry.db')
            c = conn.cursor()
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d %I:%M:%S %p")
            today_str = now.strftime("%Y-%m-%d")
            check_interval_usage = (time.time() - last_ten_min_check) >= 600

            for r in records:
                uid = r[0]
                uname = r[3]
                status = r[12]
                curr_gb = parse_gb(r[14])

                c.execute("SELECT last_status, last_usage_gb FROM user_states WHERE user_id = ?", (uid,))
                prev = c.fetchone()

                if prev is None:
                    c.execute("INSERT INTO user_states VALUES (?, ?, ?, ?, ?)", (uid, uname, status, now_str, curr_gb))
                    c.execute("INSERT INTO session_history (user_id, username, status, event_time) VALUES (?, ?, ?, ?)",
                              (uid, uname, status, now_str))
                    c.execute("INSERT OR IGNORE INTO daily_snapshots VALUES (?, ?, ?, ?)", (uid, today_str, curr_gb, 0.0))
                else:
                    prev_status = prev[0]
                    prev_gb = prev[1]

                    if prev_status != status:
                        c.execute("UPDATE user_states SET last_status = ?, last_seen = ?, last_usage_gb = ? WHERE user_id = ?",
                                  (status, now_str, curr_gb, uid))
                        c.execute("INSERT INTO session_history (user_id, username, status, event_time) VALUES (?, ?, ?, ?)",
                              (uid, uname, status, now_str))

                    if check_interval_usage:
                        delta_mb = max(0.0, (curr_gb - prev_gb) * 1024.0)
                        c.execute("INSERT INTO interval_usage (user_id, log_time, delta_mb) VALUES (?, ?, ?)",
                                  (uid, now.strftime("%I:%M %p"), round(delta_mb, 2)))
                        c.execute("UPDATE user_states SET last_usage_gb = ? WHERE user_id = ?", (curr_gb, uid))

                    c.execute("SELECT cumulative_gb FROM daily_snapshots WHERE user_id = ? AND record_date = ?", (uid, today_str))
                    snap = c.fetchone()
                    if snap is None:
                        c.execute("SELECT cumulative_gb FROM daily_snapshots WHERE user_id = ? ORDER BY record_date DESC LIMIT 1", (uid,))
                        last_snap = c.fetchone()
                        base_gb = last_snap[0] if last_snap else curr_gb
                        daily_consumed = max(0.0, curr_gb - base_gb)
                        c.execute("INSERT INTO daily_snapshots VALUES (?, ?, ?, ?)", (uid, today_str, curr_gb, round(daily_consumed, 2)))
                    else:
                        base_gb = snap[0]
                        c.execute("UPDATE daily_snapshots SET daily_consumed_gb = ? WHERE user_id = ? AND record_date = ?",
                                  (round(max(0.0, curr_gb - base_gb), 2), uid, today_str))

            if check_interval_usage:
                last_ten_min_check = time.time()

            conn.commit()
            conn.close()
        except Exception as e:
            print("Poller error:", e)

        time.sleep(180)

poller_thread = threading.Thread(target=background_poller, daemon=True)
poller_thread.start()

@app.route('/', methods=['GET'])
def root():
    return jsonify({"status": "Active", "message": "Delta Telecom Bridge Server is running on Render"})

@app.route('/api/users', methods=['GET'])
def get_subscribers():
    try:
        data = fetch_portal_data()
        return jsonify({"success": True, "total_records": len(data), "subscribers": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/user-telemetry/<user_id>', methods=['GET'])
def get_user_telemetry(user_id):
    try:
        conn = sqlite3.connect('telemetry.db')
        c = conn.cursor()

        c.execute("SELECT status, event_time FROM session_history WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,))
        rows = c.fetchall()
        logs = []
        for r in rows:
            logs.append({
                "status": r[0],
                "event_time": r[1],
                "event_type": "Disconnected (Offline)" if r[0] == 'Offline' else "Connected (Online)"
            })

        c.execute("SELECT record_date, daily_consumed_gb FROM daily_snapshots WHERE user_id = ? ORDER BY record_date ASC", (user_id,))
        snap_rows = c.fetchall()
        
        dates_labels = []
        daily_usage_gb = []
        for d, g in snap_rows:
            dates_labels.append(datetime.strptime(d, "%Y-%m-%d").strftime("%d %b"))
            daily_usage_gb.append(g)

        if not dates_labels:
            dates_labels = [datetime.now().strftime("%d %b")]
            daily_usage_gb = [0.0]

        c.execute("SELECT log_time, delta_mb FROM interval_usage WHERE user_id = ? ORDER BY id DESC LIMIT 12", (user_id,))
        interval_rows = c.fetchall()
        interval_rows.reverse()

        ten_min_labels = [r[0] for r in interval_rows]
        ten_min_data = [r[1] for r in interval_rows]

        conn.close()

        return jsonify({
            "success": True,
            "user_id": user_id,
            "monthly_labels": dates_labels,
            "daily_usage_gb": daily_usage_gb,
            "ten_min_labels": ten_min_labels,
            "ten_min_data": ten_min_data,
            "session_logs": logs
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
