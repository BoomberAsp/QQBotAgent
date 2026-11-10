"""Bounded, read-only SQL shared by JSON resources and story databases."""
import sqlite3
import time


def execute(conn, sql):
    """Execute one read query and always close the supplied connection."""
    try:
        if not sql.strip() or len(sql)>10000:
            raise ValueError('SQL不能为空且不能超过10000字')
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1000000)
        conn.execute('PRAGMA query_only=ON')
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        blocked={'load_extension','readfile','writefile'}
        conn.set_authorizer(lambda action,a,b,c,d:sqlite3.SQLITE_OK if action in allowed and not(action==sqlite3.SQLITE_FUNCTION and (b or '').lower() in blocked) else sqlite3.SQLITE_DENY)
        start=time.monotonic()
        conn.set_progress_handler(lambda:int(time.monotonic()-start>2),1000)
        cur=conn.execute(sql)
        rows=cur.fetchmany(201)
        return {'columns':[c[0] for c in cur.description or []],
                'rows':[[str(v)[:2000] if v is not None else None for v in row] for row in rows[:200]],
                'truncated':len(rows)>200}
    except sqlite3.Error as error:
        raise ValueError('查询被拒绝或执行失败：'+str(error)) from error
    finally:
        conn.close()
