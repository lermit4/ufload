import os, sys, subprocess, tempfile, hashlib
import ufload

def _run_out(args, cmd):
    return subprocess.check_output(cmd, env=pg_pass(args), stderr=subprocess.STDOUT).split('\n')
        
def _run(args, cmd, silent=False):
    if args.show:
        ufload.progress("Would run: " + str(cmd))
        rc = 0
    else:
        if silent:
            try:
                subprocess.check_output(cmd, env=pg_pass(args), stderr=subprocess.STDOUT)
            except:
                pass
            rc=0
        else:
            rc = subprocess.call(cmd, env=pg_pass(args))
    return rc

# Find exe by looking in the PATH, prefering the one
# installed by the AIO.
def _find_exe(exe):
    if sys.platform == "win32":
        path = [ 'D:\\MSF Data\\Unifield\\PostgreSQL\\bin',
                 os.environ['PATH'].split(';') ]
        bin = exe+".exe"
    else:
        path = os.environ['PATH'].split(':')
        bin = exe

    for p in path:
        fn = os.path.join(p, bin)
        if os.path.exists(fn):
            return fn
    # return the unqualified binary name and hope for
    # the best...
    return bin

def pg_common(args):
    res = []
    if args.db_host is not None:
        res.append('-h')
        res.append(args.db_host)
    if args.db_port is not None:
        res.append('-p')
        res.append(args.db_port)
    if args.db_user is not None:
        res.append('-U')
        res.append(args.db_user)
    return res

def pg_restore(args):
    return [ _find_exe('pg_restore') ] + pg_common(args)

def pg_pass(args):
    env = os.environ.copy()
    if args.db_pw is not None:
        env['PGPASSWORD'] = args.db_pw
    return env

def mkpsql(args, sql, db='postgres'):
    cmd = [ _find_exe('psql') ] + pg_common(args)
    cmd.append('-q')
    cmd.append('-t')
    cmd.append('-c')
    cmd.append(sql)
    cmd.append(db)
    return cmd

def psql(args, sql, db='postgres', silent=False):
    return _run(args, mkpsql(args, sql, db), silent)
    
def load_into(args, db, f, sz):
    tot = float(sz)
    if sz == 0:
        ufload.progress("Note: No progress percent available.")
    
    db2 = db + "_" + str(os.getpid())

    ufload.progress("Create database "+db2)
    rc = psql(args, 'CREATE DATABASE \"%s\"' % db2)
    if rc != 0:
        return rc

    # From here out, we need a try block, so that we can drop
    # the temp db if anything went wrong
    try:
        ufload.progress("Restoring into %s" % db2)
    
        cmd = pg_restore(args)
        cmd.append('--no-acl')
        cmd.append('--no-owner')
        cmd.append('-d')
        cmd.append(db2)
        cmd.append('-n')
        cmd.append('public')
    
        # Windows pg_restore gets confused when reading from a pipe,
        # so write to a temp file first.
        if sys.platform == "win32":
            tf = tempfile.NamedTemporaryFile(delete=False)
            if not args.show:
                n = 0
                next = 10
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    tf.write(chunk)
                    n += len(chunk)
                    if tot != 0:
                        pct = n/tot * 100
                        if pct > next:
                            ufload.progress("Loading data: %d%%" % int(pct))
                            next = int(pct / 10)*10 + 10

            tf.close()
            cmd.append(tf.name)

            ufload.progress("Starting restore. This will take some time.")
            try:
                rc =_run(args, cmd)
            except KeyboardInterrupt:
                raise dbException(1)

            # clean up the temp file
            try:
                os.unlink(tf.name)
            except OSError:
                pass
        else:
            # For non-Windows, feed the data in via pipe so that we have
            # some progress indication.
            if not args.show:
                p = subprocess.Popen(cmd, bufsize=1024*1024*10,
                                     stdin=subprocess.PIPE,
                                     stdout=sys.stdout,
                                     stderr=sys.stderr,
                                     env=pg_pass(args))

                n = 0
                next = 10
                for chunk in iter(lambda: f.read(8192), b''):
                    try:
                        p.stdin.write(chunk)
                    except IOError:
                        break
                    n += len(chunk)
                    if tot != 0:
                        pct = n/tot * 100
                        if pct > next:
                            ufload.progress("Restoring: %d%%" % int(pct))
                            next = int(pct / 10)*10 + 10

                p.stdin.close()
                ufload.progress("Restoring: 100%")
                ufload.progress("Waiting for Postgres to finish restore")
                rc = p.wait()
            else:
                ufload.progress("Would run: "+ str(cmd))
                rc = 0

        rcstr = "ok"
        if rc != 0:
            rcstr = "error %d" % rc
        ufload.progress("Restore finished with result code: %s" % rcstr)
        _checkrc(rc)

        _checkrc(delive(args, db2))
        
        ufload.progress("Drop database "+db)
        killCons(args, db)
        rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"'%db)
        _checkrc(rc)

        ufload.progress("Rename database %s to %s" % (db2, db))
        rc = psql(args, 'ALTER DATABASE \"%s\" RENAME TO \"%s\"'%(db2, db))
        _checkrc(rc)
        
        return 0
    except dbException as e:
        # something went wrong, so drop the temp table
        ufload.progress("Cleanup: dropping table %s" % db2)
        killCons(args, db2)
        psql(args, 'DROP DATABASE \"%s\"'%db2)
        return e.rc

# De-live uses psql to change a restored database taken from a live backup
# into a non-production, non-live database. It:
# 1. stomps all existing passwords
# 2. changes the sync connection to a local one
# 3. removes cron jobs for backups and sync
# 4. set the backup directory
def delive(args, db):
    if args.live:
        ufload.progress("*** WARNING: The restored database has LIVE passwords and LIVE syncing.")
        return 0
    
    # set the username of the admin account
    adminuser = args.adminuser.lower()
    rc = psql(args, 'update res_users set login = \'%s\' where id = 1;' % adminuser, db)
    if rc != 0:
        return rc

    # put the chosen password into all users
    rc = psql(args, 'update res_users set password = \'%s\';' % args.adminpw, db)
    if rc != 0:
        return rc

    # change the sync config to local
    if args.db_prefix:
        pfx = args.db_prefix
    else:
        pfx = ''
    rc = psql(args, 'update sync_client_sync_server_connection set protocol = \'xmlrpc\', login = \'%s\', database = \'%sSYNC_SERVER_LOCAL\', host = \'127.0.0.1\', port = 8069;' % (adminuser, pfx), db)
    if rc != 0:
        return rc

    # disable cron jobs
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'backup.config\';', db)
    if rc != 0:
        return rc
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'sync.client.entity\';', db)
    if rc != 0:
        return rc
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'stock.mission.report\';', db)
    if rc != 0:
        return rc

    # Set the backup directory
    directory = "E'd:\\\\'"
    if sys.platform != "win32" and args.db_host in [ None, 'ct0', 'localhost' ]:
        # when loading on non-windows, to a local database, use /tmp
        directory = '\'/tmp\''
    
    rc = psql(args, 'update backup_config set beforemanualsync=\'f\', beforepatching=\'f\', aftermanualsync=\'f\', beforeautomaticsync=\'f\', afterautomaticsync=\'f\', name = %s;' % directory, db)
    if rc != 0:
        return rc

    # ok, delive finished with no problems
    return 0

def _checkrc(rc):
    if rc != 0:
        raise dbException(rc)

class dbException(Exception):
    def __init__(self, rc):
        self.rc = rc

def ver(args):
    v = _run_out(args, mkpsql(args, 'show server_version'))
    return v

def killCons(args, db):
    # For Postgres 8, it is procpid, for 9 it is pid
    v = ver(args)
    if len(v) > 1 and ' 9.' in v[0]:
        col = 'pid'
    else:
        col = 'procpid'

    cmd = mkpsql(args, 'select %s from pg_stat_activity where datname = \'%s\';' % (col, db), 'postgres')
    for i in _run_out(args, cmd):
        try:
            pid = int(i)
            psql(args, 'select pg_terminate_backend(%s)' % pid, 'postgres', True)
        except ValueError:
            # skip lines which are not numbers
            pass

def get_hwid(args):
    if sys.platform == 'win32':
        import _winreg
        try:
            with _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE,
                                 "SYSTEM\ControlSet\services\eventlog\Application\openerp-web-6.0",
                                 0, _winreg.KEY_READ) as registry_key:
                hwid, regtype = _winreg.QueryValueEx(registry_key, "HardwareId")
                ufload.progress("Hardware id from registry key: %s" % hwid)
                return hwid
        except WindowsError:
            return None
    else:
        # Follow the same algorithm that Unifield uses (see sync_client.py)
        mac = []
        for line in os.popen("/sbin/ifconfig"):
            if line.find('Ether') > -1:
                mac.append(line.split()[4])
                    
        mac.sort()
        hw_hash = hashlib.md5(''.join(mac)).hexdigest()
        return hw_hash

def _db_to_instance(args, db):
    if args.db_prefix:
        db = db[len(args.db_prefix)+1:]
    return '_'.join(db.split('_')[0:-2])

def sync_link(args, hwid, db, sdb):
    return psql(args, 'update sync_server_entity set hardware_id = \'%s\' where name = \'%s\';' % (hwid, _db_to_instance(args, db)), sdb)

# Remove all databases which come from the same instance as one of the instances
# in dbs, but which are not in dbs.
def clean(args, dbs):
    toClean = {}
    toKeep = {}
    for d in dbs:
        i = _db_to_instance(args, d)
        toClean[i] = True
        toKeep[d] = True
        
    for d in _allDbs(args):
        i = _db_to_instance(args, d)
        if d not in toKeep and i in toClean:
            ufload.progress("Cleaning other database for instance %s: %s" % (i, d))
            killCons(args, d)
            rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"'%d)
            if rc != 0:
                return rc
    return 0            

def _allDbs(args):
    if args.db_user:
        v = _run_out(args, mkpsql(args, 'select datname from pg_database where datdba=(select usesysid from pg_user where usename=\'%s\') and datistemplate = false and datname != \'postgres\'' % args.db_user))
    else:
        v = _run_out(args, mkpsql(args, 'select datname from pg_database where datistemplate = false and datname != \'postgres\''))
        
    return map(lambda x: x.strip(), filter(len, v))

def exists(args, db):
    v = _run_out(args, mkpsql(args, 'select datname from pg_database where datname = \'%s\'' % db))
    v = filter(len, map(lambda x: x.strip(), v))
    return len(v)==1 and v[0] == db

# These two functions read and write from a little "about" table
# where we store the size of the input file, which helps us avoid
# reloading the sync server when we don't need to.
def get_sync_server_len(args, db='SYNC_SERVER_LOCAL'):
    try:
        l = _run_out(args, mkpsql(args, 'select length from about', db))
        return int(filter(len, l)[0])
    except subprocess.CalledProcessError:
        pass
    return -1

def write_sync_server_len(args, l, db='SYNC_SERVER_LOCAL'):
    _run_out(args, mkpsql(args, 'drop table if exists about; create table about ( length int ); insert into about values ( %d )' % l, db))

