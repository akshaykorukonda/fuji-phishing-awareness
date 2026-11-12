
import os,sqlite3,secrets,hashlib,smtplib,json,io,csv
from datetime import datetime,timezone,timedelta
from email.message import EmailMessage
from functools import wraps
from flask import Flask,request,session,redirect,url_for,render_template,abort,Response,jsonify
app=Flask(__name__); app.secret_key=os.environ.get('SECRET_KEY','CHANGE_ME_LONG_RANDOM_SECRET')
DB=os.environ.get('DATABASE_PATH','responses.sqlite3'); CORP='fujisilvertech.com'
OWNER=os.environ.get('INITIAL_ADMIN_EMAIL','Akshay.Korukonda@smartims.com').lower()
ADMINS={OWNER}|{x.strip().lower() for x in os.environ.get('ADMIN_EMAILS','').split(',') if x.strip()}
Q=json.load(open('questions.json'))
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
with conn() as c:
 c.execute('CREATE TABLE IF NOT EXISTS codes(email text,hash text,expires text,used integer default 0)')
 c.execute('CREATE TABLE IF NOT EXISTS attempts(id integer primary key,email text,started text,completed text,duration integer,score integer,answers text)')
def corp(e): return e.count('@')==1 and e.rsplit('@',1)[1].lower()==CORP
def mail_code(email,code):
 host=os.environ.get('SMTP_HOST'); user=os.environ.get('SMTP_USER'); pw=os.environ.get('SMTP_PASS')
 if not all([host,user,pw]): raise RuntimeError('Email delivery is not configured. Set SMTP_HOST, SMTP_USER and SMTP_PASS.')
 m=EmailMessage(); m['Subject']='Fuji Silvertech awareness sign-in code'; m['From']=os.environ.get('FROM_EMAIL',user); m['To']=email
 m.set_content('Your sign-in code is '+code+'. It expires in ten minutes.')
 with smtplib.SMTP(host,int(os.environ.get('SMTP_PORT','587'))) as s: s.starttls(); s.login(user,pw); s.send_message(m)
def signed(fn):
 @wraps(fn)
 def w(*a,**k):
  if not session.get('email'): return redirect('/')
  return fn(*a,**k)
 return w
def admin(fn):
 @wraps(fn)
 def w(*a,**k):
  if session.get('email','').lower() not in ADMINS: abort(403)
  return fn(*a,**k)
 return w
@app.route('/',methods=['GET','POST'])
def home():
 msg=''
 if request.method=='POST':
  e=request.form['email'].strip().lower()
  if not corp(e) and e not in ADMINS: msg='Only verified @fujisilvertech.com employees or explicitly allowlisted administrators may sign in.'
  else:
   code=f'{secrets.randbelow(1000000):06d}'
   with conn() as c: c.execute('insert into codes values(?,?,?,0)',(e,hashlib.sha256(code.encode()).hexdigest(),(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()))
   try: mail_code(e,code); session['pending']=e; return redirect('/verify')
   except Exception as ex: msg=str(ex)
 return render_template('home.html',msg=msg)
@app.route('/verify',methods=['GET','POST'])
def verify():
 if not session.get('pending'): return redirect('/')
 msg=''
 if request.method=='POST':
  e=session['pending']; code=request.form['code']
  with conn() as c:
   r=c.execute('select rowid,* from codes where email=? and used=0 order by rowid desc limit 1',(e,)).fetchone()
   if r and r['expires']>datetime.now(timezone.utc).isoformat() and secrets.compare_digest(r['hash'],hashlib.sha256(code.encode()).hexdigest()):
    c.execute('update codes set used=1 where rowid=?',(r['rowid'],)); session['email']=e; session.pop('pending'); return redirect('/admin' if e in ADMINS else '/quiz')
  msg='Invalid or expired code.'
 return render_template('verify.html',msg=msg)
@app.route('/quiz')
@signed
def quiz():
 if not corp(session['email']): abort(403)
 with conn() as c:
  c.execute('insert into attempts(email,started) values(?,?)',(session['email'],datetime.now(timezone.utc).isoformat()))
  session['attempt']=c.execute('select last_insert_rowid()').fetchone()[0]
 return render_template('quiz.html',questions=Q,email=session['email'])
@app.route('/submit',methods=['POST'])
@signed
def submit():
 a=request.json.get('answers',[])
 if len(a)!=10 or any(not isinstance(x,int) or x not in range(4) for x in a): return jsonify(error='Answer all ten questions.'),400
 score=sum(a[i]==Q[i]['correct'] for i in range(10)); aid=session.get('attempt')
 with conn() as c:
  r=c.execute('select started from attempts where id=? and email=?',(aid,session['email'])).fetchone()
  if not r: abort(403)
  now=datetime.now(timezone.utc); duration=int((now-datetime.fromisoformat(r['started'])).total_seconds())
  c.execute('update attempts set completed=?,duration=?,score=?,answers=? where id=?',(now.isoformat(),duration,score*10,json.dumps(a),aid))
 return jsonify(url='/result/'+str(aid))
@app.route('/result/<int:aid>')
@signed
def result(aid):
 with conn() as c: r=c.execute('select * from attempts where id=? and email=?',(aid,session['email'])).fetchone()
 if not r: abort(404)
 return render_template('result.html',r=r,q=Q,a=json.loads(r['answers']))
@app.route('/admin')
@admin
def dashboard():
 with conn() as c: rows=c.execute('select * from attempts where completed is not null order by completed desc').fetchall()
 return render_template('admin.html',rows=rows,avg=round(sum(x['score'] for x in rows)/len(rows),1) if rows else 0)
@app.route('/admin/export')
@admin
def export():
 out=io.StringIO(); w=csv.writer(out); w.writerow(['Email','Attempt','Started','Completed','Duration','Score']+['Q'+str(i) for i in range(1,11)])
 with conn() as c: rows=c.execute('select * from attempts where completed is not null').fetchall()
 for r in rows: w.writerow([r['email'],r['id'],r['started'],r['completed'],r['duration'],r['score']]+json.loads(r['answers']))
 return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=fuji-awareness.csv'})
@app.route('/logout')
def logout(): session.clear(); return redirect('/')
