from getpass import getpass
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import init_db, connect
from app.security import hash_password

init_db()
username = input('管理员用户名: ').strip()
password = getpass('管理员密码（至少12位）: ')
if len(username) < 3 or len(password) < 12:
    raise SystemExit('用户名至少3位，密码至少12位。')
con = connect()
try:
    con.execute('INSERT INTO admin_users(username,password_hash) VALUES(?,?)', (username, hash_password(password)))
    con.commit()
except Exception as e:
    raise SystemExit(f'创建失败: {e}')
finally:
    con.close()
print('管理员创建完成。')
