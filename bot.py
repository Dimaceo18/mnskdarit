import asyncio, os, logging, secrets, signal, csv, io
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message, CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.exceptions import TelegramAPIError

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ['BOT_TOKEN']; ADMIN=int(os.environ['ADMIN_ID']); CHANNEL=int(os.environ['CHANNEL_ID'])
LINK=os.getenv('CHANNEL_INVITE_LINK','https://t.me/+lwdIQrNOfaU5NmMy')
bot=Bot(TOKEN); dp=Dispatcher(); db=None; username=''
SCHEMA='''CREATE TABLE IF NOT EXISTS rounds(id bigserial PRIMARY KEY, test boolean NOT NULL, ends timestamptz NOT NULL, status text NOT NULL DEFAULT 'active');
CREATE UNIQUE INDEX IF NOT EXISTS one_active ON rounds ((1)) WHERE status='active';
CREATE TABLE IF NOT EXISTS players(round_id bigint REFERENCES rounds(id) ON DELETE CASCADE, uid bigint, ref bigint, eligible_new boolean NOT NULL, joined timestamptz, PRIMARY KEY(round_id,uid));
CREATE TABLE IF NOT EXISTS links(round_id bigint REFERENCES rounds(id) ON DELETE CASCADE, uid bigint, link text UNIQUE, PRIMARY KEY(round_id,uid));
CREATE TABLE IF NOT EXISTS join_link_history(round_id bigint REFERENCES rounds(id) ON DELETE CASCADE, uid bigint NOT NULL, link text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS winners(round_id bigint REFERENCES rounds(id) ON DELETE CASCADE, uid bigint, prize text, notified boolean DEFAULT false, PRIMARY KEY(round_id,uid));'''
SCHEMA += """
CREATE TABLE IF NOT EXISTS gift_settings(key text PRIMARY KEY, value text NOT NULL);
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS prize_list text[];
ALTER TABLE players ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE winners ADD COLUMN IF NOT EXISTS delivered boolean DEFAULT false;
CREATE TABLE IF NOT EXISTS gift_reminders(round_id bigint REFERENCES rounds(id) ON DELETE CASCADE, uid bigint, hours int, PRIMARY KEY(round_id,uid,hours));
CREATE TABLE IF NOT EXISTS gift_publications(round_id bigint PRIMARY KEY REFERENCES rounds(id) ON DELETE CASCADE);
"""
DEFAULT_PRIZES=['Яндекс Станция Мини']+['Сертификат WB 50 BYN']*10+['Подарок Telegram']*50
async def setting(key,default=''):
 return await db.fetchval('SELECT value FROM gift_settings WHERE key=$1',key) or default
async def save_setting(key,value):
 await db.execute('INSERT INTO gift_settings VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',key,value)
async def configured_prizes():
 import json
 return json.loads(await setting('prizes',json.dumps(DEFAULT_PRIZES)))
def prize_text(items):
 from collections import Counter
 return '\n'.join(f'{n} × {name}' for name,n in Counter(items).items())
def kb(*rows):
 return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, **({'url':v} if v.startswith('https://') else {'callback_data':v})) for t,v in row] for row in rows])
async def active(): return await db.fetchrow("SELECT * FROM rounds WHERE status='active'")
async def member(uid):
 m=await bot.get_chat_member(CHANNEL,uid)
 return m.status in ('member','administrator','creator') or (m.status=='restricted' and m.is_member)
def weight(n): return 1+n+3*(n>=5)+7*(n>=10)+15*(n>=20)
async def counts(r):
 cutoff=datetime.now(timezone.utc)-timedelta(hours=0 if r['test'] else 24)
 rows=await db.fetch('SELECT * FROM players WHERE round_id=$1 AND joined IS NOT NULL',r['id'])
 valid={}
 for p in rows:
  try:
   if await member(p['uid']):valid[p['uid']]=p
  except TelegramRetryAfter as e:
   await asyncio.sleep(e.retry_after+1)
   if await member(p['uid']):valid[p['uid']]=p
 refs={u:0 for u in valid}
 for p in valid.values():
  if p['eligible_new'] and p['joined']<=cutoff and p['ref'] in refs: refs[p['ref']]+=1
 return refs
async def show(m,uid):
 r=await active()
 if not r or r['ends']<=datetime.now(timezone.utc): return await m.answer('Сейчас нет активного розыгрыша.')
 p=await db.fetchrow('SELECT * FROM players WHERE round_id=$1 AND uid=$2',r['id'],uid)
 if not p: return await m.answer('Отправьте /start для регистрации.')
 if not await member(uid):
  status=await bot.get_chat_member(CHANNEL,uid)
  if status.status=='kicked':
   logging.warning('User %s is banned from channel',uid)
   return await m.answer('Ваш аккаунт находится в списке удалённых или заблокированных пользователей канала. Обратитесь к администратору канала.')
  # Generate a fresh link on every request, retaining the previous link mapping
  # until the new link has been created successfully.
  try:
   invite=await bot.create_chat_invite_link(CHANNEL,creates_join_request=True)
   logging.info('Invite metadata: round=%s revoked=%s expires=%s join_request=%s',r['id'],invite.is_revoked,invite.expire_date,invite.creates_join_request)
   link=await db.fetchval('INSERT INTO links VALUES($1,$2,$3) ON CONFLICT(round_id,uid) DO UPDATE SET link=EXCLUDED.link RETURNING link',r['id'],uid,invite.invite_link)
   await db.execute('INSERT INTO join_link_history VALUES($1,$2,$3) ON CONFLICT DO NOTHING',r['id'],uid,link)
   logging.info('Fresh join link created for round %s',r['id'])
  except TelegramAPIError:
   logging.exception('Could not create join link')
   return await m.answer('Не удалось создать ссылку. Попробуйте обновить её.',reply_markup=kb([('Обновить ссылку','check')]))
  return await m.answer('Подайте заявку на вступление в Минский ТГ. Бот автоматически одобрит её и начислит билет. Подайте заявку до окончания розыгрыша.',reply_markup=kb([('Подписаться на канал',link)],[('Обновить ссылку / проверить подписку','check')]))
 await db.execute('UPDATE players SET joined=COALESCE(joined,now()) WHERE round_id=$1 AND uid=$2',r['id'],uid)
 # Only query this participant’s referrals, not the entire audience.
 cutoff=datetime.now(timezone.utc)-timedelta(hours=0 if r['test'] else 24)
 n=await db.fetchval('SELECT count(*) FROM players WHERE round_id=$1 AND ref=$2 AND eligible_new AND joined<=$3',r['id'],uid,cutoff)
 url=f'https://t.me/{username}?start=r{r["id"]}_{uid}'
 share='https://t.me/share/url?'+urlencode({'url':url,'text':'Участвуй в розыгрыше Минский ТГ!'})
 remaining=max(0,int((r['ends']-datetime.now(timezone.utc)).total_seconds()))
 progress=next((f'До бонуса за {k} друзей: ещё {k-n}' for k in (5,10,20) if n<k),'Все бонусы получены!')
 await m.answer(('🧪 ПРОБНЫЙ РОЗЫГРЫШ: призы виртуальные\n\n' if r['test'] else '')+f'🎁 Минский ТГ дарит подарки!\n\n🎟 Ваши билеты: {weight(n)}\n👥 Подтверждённых друзей: {n}\n{progress}\n\nДо итогов: {remaining//3600} ч {(remaining%3600)//60} мин\nИтоги: {r["ends"].astimezone(timezone(timedelta(hours=3))):%d.%m %H:%M} (Минск)\n\nВаша ссылка:\n{url}',reply_markup=kb([('👥 Пригласить друга',share)],[('🎟 Мои билеты','check'),('🎁 Призы','prizes')],[('📋 Правила','rules'),('🏆 Результаты','public_results')]))
@dp.message(CommandStart())
async def start(m:Message):
 r=await active(); uid=m.from_user.id
 if not r or r['ends']<=datetime.now(timezone.utc): return await m.answer('Сейчас нет активного розыгрыша.')
 ref=None; source=None; parts=m.text.split(maxsplit=1)
 if len(parts)>1 and parts[1].startswith('s_'):
  import re
  if re.fullmatch(r's_[a-zA-Z0-9_-]{1,40}',parts[1]): source=parts[1][2:]
 if len(parts)>1 and parts[1].startswith('r'):
  try:
   rid,ref=map(int,parts[1][1:].split('_')); assert parts[1].startswith('r') and rid==r['id'] and ref!=uid
   if not await db.fetchval('SELECT 1 FROM players WHERE round_id=$1 AND uid=$2 AND joined IS NOT NULL',rid,ref): ref=None
  except (ValueError,AssertionError): ref=None
 existing=await member(uid)
 await db.execute('INSERT INTO players(round_id,uid,ref,eligible_new,joined,source) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING',r['id'],uid,ref,not existing,datetime.now(timezone.utc) if existing else None,source)
 banner=await setting('banner')
 if banner:
  try: await m.answer_photo(banner)
  except TelegramAPIError: logging.exception('banner')
 await show(m,uid)
@dp.callback_query(F.data=='check')
async def check(c:CallbackQuery):
 await c.answer(); await show(c.message,c.from_user.id)
@dp.callback_query(F.data=='prizes')
async def prizes(c:CallbackQuery):
 await c.answer(); r=await active()
 await c.message.answer('Тест: 3 виртуальных приза.' if r and r['test'] else prize_text((r['prize_list'] if r else None) or await configured_prizes()))
@dp.callback_query(F.data=='rules')
async def rules(c:CallbackQuery):
 await c.answer(); await c.message.answer('📋 Правила\n1. Подпишитесь на Минский ТГ: 1 билет.\n2. Каждый новый подтверждённый друг: +1 билет.\n3. Бонусы за 5, 10 и 20 друзей: +3, +7 и +15 (суммируются).\n4. Один приз на участника. Перед итогами проверяется подписка.\n5. В реальном розыгрыше друг должен оставаться подписчиком не менее 24 часов до итогов. В тесте подарки виртуальные.')
@dp.callback_query(F.data=='public_results')
async def public_results(c:CallbackQuery):
 await c.answer(); rid=await db.fetchval("SELECT max(id) FROM rounds WHERE status='finished'")
 if not rid:return await c.message.answer('Итогов пока нет.')
 prize=await db.fetchval('SELECT prize FROM winners WHERE round_id=$1 AND uid=$2',rid,c.from_user.id)
 await c.message.answer(f'Последний завершённый розыгрыш #{rid}. '+(f'Ваш приз: {prize}' if prize else 'Вы не выиграли в этом розыгрыше.'))
class PrivateReply:
 def __init__(self,uid): self.uid=uid
 async def answer(self,text,**kwargs): return await bot.send_message(self.uid,text,**kwargs)
@dp.chat_join_request()
async def join(j:ChatJoinRequest):
 logging.info('Join request received: chat=%s user=%s',j.chat.id,j.from_user.id)
 if j.chat.id!=CHANNEL or not j.invite_link: return
 p=await db.fetchrow("SELECT l.*,r.test,r.ends FROM (SELECT round_id,uid,link FROM links UNION SELECT round_id,uid,link FROM join_link_history) l JOIN rounds r ON r.id=l.round_id WHERE l.link=$1 AND r.status='active'",j.invite_link.invite_link)
 if not p or p['uid']!=j.from_user.id or p['ends']<=datetime.now(timezone.utc):
  logging.warning('Join request not approved: unmatched user/link or expired round')
  return
 await bot.approve_chat_join_request(CHANNEL,j.from_user.id)
 logging.info('Join request approved: user=%s round=%s',j.from_user.id,p['round_id'])
 await db.execute('UPDATE players SET joined=COALESCE(joined,now()) WHERE round_id=$1 AND uid=$2',p['round_id'],p['uid'])
 try:
  await bot.send_message(p['uid'],'✅ Подписка подтверждена! Ваш билет начислен.')
  await show(PrivateReply(p['uid']),p['uid'])
 except TelegramAPIError: logging.exception('join notification')
 ref=await db.fetchval('SELECT ref FROM players WHERE round_id=$1 AND uid=$2 AND eligible_new',p['round_id'],p['uid'])
 if ref:
  try:
   await bot.send_message(ref,'🎉 Ваш друг вступил в канал! '+('Дополнительный билет начислен при действующей подписке.' if p['test'] else 'Билет будет подтверждён после 24 часов подписки друга.'))
   await show(PrivateReply(ref),ref)
  except TelegramAPIError: logging.exception('referral notification')
@dp.chat_member()
async def change(e:ChatMemberUpdated):
 if e.chat.id!=CHANNEL: return
 uid=e.new_chat_member.user.id
 if e.new_chat_member.status in ('left','kicked'):
  await db.execute("UPDATE players SET joined=NULL WHERE uid=$1 AND round_id IN (SELECT id FROM rounds WHERE status='active')",uid)
@dp.message(Command('admin'))
async def admin(m:Message):
 if m.from_user.id!=ADMIN:return
 r=await active()
 await m.answer('Управление: '+(f'активен #{r["id"]}' if r else 'нет розыгрыша'),reply_markup=kb([('🧪 30 минут','test'),('🧪 2 часа','test120')],[('🧪 24 часа','test1440')],[('Статистика','stats'),('Завершить тест','finish')],[('📥 CSV участников','export')],[('Удалить тесты','purge')],[('Реальный розыгрыш','live')],[('Настройки V6','settings6')],[('Источники','sources6'),('Выдача призов','delivery6')]))
@dp.message(Command('diagnostics'))
async def diagnostics(m:Message):
 if m.from_user.id!=ADMIN:return
 try:
  me=await bot.get_me(); chat=await bot.get_chat(CHANNEL); rights=await bot.get_chat_member(CHANNEL,me.id); await db.fetchval('SELECT 1')
  await m.answer(f'Telegram и PostgreSQL: OK\nКанал: {chat.title}\nСтатус: {rights.status}\nПриглашения: {getattr(rights,"can_invite_users",False)}\nБлокировка запуска: активна')
 except Exception as e: await m.answer(f'Ошибка диагностики: {type(e).__name__} {str(e)[:300]}')
async def finish(r):
 refs=await counts(r); pool=dict(refs); result=[]
 prizes=['Тестовый приз '+str(i) for i in range(1,4)] if r['test'] else (r['prize_list'] or DEFAULT_PRIZES)
 for prize in prizes:
  if not pool:break
  x=secrets.randbelow(sum(weight(n) for n in pool.values()))
  for uid,n in pool.items():
   x-=weight(n)
   if x<0:break
  result.append((r['id'],uid,prize)); del pool[uid]
 async with db.acquire() as c:
  async with c.transaction():
   status=await c.fetchval("UPDATE rounds SET status='finished' WHERE id=$1 AND status='active' RETURNING id",r['id'])
   if not status:return
   await c.executemany('INSERT INTO winners(round_id,uid,prize) VALUES($1,$2,$3)',result)
 await bot.send_message(ADMIN,f'Розыгрыш #{r["id"]} завершён. Победителей: {len(result)}. /results')
@dp.message(Command('results'))
async def results(m:Message):
 if m.from_user.id!=ADMIN:return
 rows=await db.fetch('SELECT * FROM winners WHERE round_id=(SELECT max(id) FROM rounds WHERE status=\'finished\') ORDER BY uid')
 await m.answer('\n'.join(f'{p["uid"]}: {p["prize"]}' for p in rows)[:3900] or 'Нет результатов')
@dp.callback_query(F.data.in_({'test','test120','test1440','stats','finish','purge','live','live_yes','export'}))
async def control(c:CallbackQuery):
 if c.from_user.id!=ADMIN:return await c.answer('Нет доступа',show_alert=True)
 await c.answer(); r=await active(); a=c.data
 if a=='live':
  if r:return await c.message.answer('Сначала завершите активный розыгрыш.')
  if os.getenv('ENABLE_LIVE')!='1':return await c.message.answer('Реальный запуск заблокирован. После проверки условий установите ENABLE_LIVE=1 в Render.')
  return await c.message.answer('Для запуска укажите дату окончания: /live_date 31.12.2026 20:00 (Минск). Затем подтвердите запуск.')
 if a in ('test','test120','test1440'):
  if r:return await c.message.answer('Уже есть активный розыгрыш.')
  await db.execute("INSERT INTO rounds(test,ends) VALUES(true,now()+$1::interval)",timedelta(minutes={'test':30,'test120':120,'test1440':1440}[a]))
  return await c.message.answer('Тест запущен. /start')
 if a=='export':
  rid=r['id'] if r else await db.fetchval('SELECT max(id) FROM rounds')
  if not rid:return await c.message.answer('Нет данных.')
  rows=await db.fetch('SELECT uid,ref,eligible_new,joined,source FROM players WHERE round_id=$1 ORDER BY uid',rid)
  out=io.StringIO(); writer=csv.writer(out); writer.writerow(['user_id','referrer_id','new_subscriber','joined_at','source']); writer.writerows([tuple(row) for row in rows])
  return await c.message.answer_document(BufferedInputFile(out.getvalue().encode('utf-8-sig'),filename=f'round_{rid}.csv'))
 if a=='stats':
  if not r:return await c.message.answer('Нет активного розыгрыша.')
  n=await db.fetchval('SELECT count(*) FROM players WHERE round_id=$1',r['id']); confirmed=await db.fetchval('SELECT count(*) FROM players WHERE round_id=$1 AND joined IS NOT NULL',r['id']); referred=await db.fetchval('SELECT count(*) FROM players WHERE round_id=$1 AND joined IS NOT NULL AND eligible_new AND ref IS NOT NULL',r['id']); return await c.message.answer(f'Регистраций: {n}\nПодтверждённых по базе: {confirmed}\nНовых по реферальным ссылкам: {referred}')
 if a=='finish' and r and r['test']:await finish(r);return
 if a=='purge':
  if r:return await c.message.answer('Сначала завершите активный тест.')
  await db.execute('DELETE FROM rounds WHERE test');return await c.message.answer('Тестовые данные удалены. Подписчики не удалены.')
async def jobs():
 while True:
  try:
   r=await active()
   if r and r['ends']<=datetime.now(timezone.utc):await finish(r)
   for p in await db.fetch('SELECT w.*,r.test FROM winners w JOIN rounds r ON r.id=w.round_id WHERE NOT notified LIMIT 50'):
    if not p['test'] and not await setting('live_enabled'):continue
    # Notification only, never a paid gift.
    try:await bot.send_message(p['uid'],(f'🧪 Тест: вы выиграли {p["prize"]}. Реальный подарок не отправляется.' if p['test'] else f'🎉 Вы выиграли: {p["prize"]}! Ожидайте инструкции организатора.'))
    except TelegramAPIError:logging.exception('notification failed');continue
    await db.execute('UPDATE winners SET notified=true WHERE round_id=$1 AND uid=$2',p['round_id'],p['uid'])
  except Exception:logging.exception('scheduler')
  await asyncio.sleep(5)

@dp.callback_query(F.data.in_({'settings6','sources6','delivery6','confirm6','broadcast6','publish6'}))
async def admin6(c:CallbackQuery):
 if c.from_user.id!=ADMIN:return await c.answer('Нет доступа',show_alert=True)
 await c.answer(); a=c.data
 if a=='settings6':
  return await c.message.answer('Настройки V6\n/prize_set — призы (каждая строка: количество | название)\n/banner — отправьте фото с этой подписью\n/source minsk_news — рекламная ссылка\n/broadcast текст — предпросмотр рассылки\n/delivered ID_розыгрыша ID_победителя — отметить выдачу\n/publish — опубликовать последние реальные итоги вручную\n/auto_publish on — автоматическая публикация\n/auto_publish off — отключить',reply_markup=kb([('Источники','sources6'),('Выдача','delivery6')]))
 if a=='sources6':
  r=await active()
  if not r:return await c.message.answer('Нет активного розыгрыша.')
  rows=await db.fetch("SELECT COALESCE(source,'без метки') AS src,count(*) AS n,count(*) FILTER(WHERE eligible_new AND joined IS NOT NULL) AS new FROM players WHERE round_id=$1 GROUP BY 1",r['id'])
  return await c.message.answer(('\n'.join(f'{x["src"]}: регистрации {x["n"]}, новые подписки {x["new"]}' for x in rows) or 'Нет данных')[:3900])
 if a=='delivery6':
  rows=await db.fetch('SELECT w.* FROM winners w JOIN rounds r ON r.id=w.round_id WHERE NOT r.test AND NOT delivered ORDER BY round_id,uid LIMIT 50')
  return await c.message.answer(('\n'.join(f'{x["round_id"]} / {x["uid"]}: {x["prize"]}' for x in rows) or 'Нет невыданных призов')[:3900])
 if a=='confirm6':
  if os.getenv('ENABLE_LIVE')!='1' or await active():return await c.message.answer('Запуск недоступен.')
  raw=await setting('pending_end')
  if not raw:return await c.message.answer('Сначала задайте дату.')
  end=datetime.fromisoformat(raw)
  if end<=datetime.now(timezone.utc)+timedelta(hours=24):return await c.message.answer('До итогов должно быть больше 24 часов.')
  await db.execute('INSERT INTO rounds(test,ends,prize_list) VALUES(false,$1,$2)',end,await configured_prizes())
  await save_setting('pending_end',''); await save_setting('live_enabled','1')
  return await c.message.answer('Реальный розыгрыш запущен. /start')
 if a=='broadcast6':
  msg=await setting('pending_broadcast'); r=await active()
  if not msg or not r:return await c.message.answer('Нет рассылки или активного розыгрыша.')
  await save_setting('pending_broadcast','')
  rows=await db.fetch('SELECT uid FROM players WHERE round_id=$1',r['id']); ok=0
  for x in rows:
   if await safe_send(x['uid'],msg):ok+=1
   await asyncio.sleep(.08)
  return await c.message.answer(f'Доставлено: {ok} из {len(rows)}')
 if a=='publish6':
  rid=await db.fetchval("SELECT max(id) FROM rounds WHERE NOT test AND status='finished'")
  if not rid:return await c.message.answer('Нет реальных итогов.')
  await publish_round(rid); return await c.message.answer('Итоги обработаны.')
async def safe_send(uid,msg):
 for _ in range(3):
  try:await bot.send_message(uid,msg);return True
  except TelegramRetryAfter as e:await asyncio.sleep(e.retry_after+1)
  except TelegramAPIError:return False
 return False
@dp.message(Command('prize_set','source','live_date','broadcast','delivered','publish','auto_publish'))
async def commands6(m:Message):
 if m.from_user.id!=ADMIN:return
 cmd,_,arg=m.text.partition(' '); cmd=cmd.split('@')[0]; arg=arg.strip()
 try:
  if cmd=='/prize_set':
   if await active():return await m.answer('Редактирование призов только между розыгрышами.')
   items=[]
   for line in arg.splitlines():
    n,name=line.split('|',1); n=int(n); name=name.strip()
    if not 1<=n<=1000 or not name or len(name)>100 or len(items)+n>1000:raise ValueError()
    items.extend([name]*n)
   if not items:raise ValueError()
   import json
   await save_setting('prizes',json.dumps(items));return await m.answer('Сохранено:\n'+prize_text(items))
  if cmd=='/source':
   import re
   if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',arg):raise ValueError()
   return await m.answer(f'https://t.me/{username}?start=s_{arg}')
  if cmd=='/live_date':
   if await active() or os.getenv('ENABLE_LIVE')!='1':return await m.answer('Реальный запуск недоступен.')
   end=datetime.strptime(arg,'%d.%m.%Y %H:%M').replace(tzinfo=timezone(timedelta(hours=3)))
   if end<=datetime.now(timezone.utc)+timedelta(hours=24):raise ValueError()
   await save_setting('pending_end',end.isoformat());return await m.answer(f'Итоги: {arg} (Минск)\n'+prize_text(await configured_prizes()),reply_markup=kb([('Подтвердить реальный запуск','confirm6')]))
  if cmd=='/broadcast':
   if not arg or len(arg)>3500:raise ValueError()
   await save_setting('pending_broadcast',arg);return await m.answer('Предпросмотр:\n'+arg,reply_markup=kb([('Отправить участникам','broadcast6')]))
  if cmd=='/delivered':
   rid,uid=map(int,arg.split()); result=await db.execute('UPDATE winners SET delivered=true WHERE round_id=$1 AND uid=$2',rid,uid);return await m.answer(result)
  if cmd=='/publish':return await m.answer('Подтвердите публикацию последних реальных итогов.',reply_markup=kb([('Опубликовать','publish6')]))
  if cmd=='/auto_publish':
   if arg not in ('on','off'):raise ValueError()
   await save_setting('auto_publish',arg);return await m.answer('Сохранено.')
 except ValueError:return await m.answer('Неверный формат. /admin → Настройки V6')
@dp.message(F.photo)
async def banner6(m:Message):
 if m.from_user.id==ADMIN and (m.caption or '').strip()=='/banner':
  await save_setting('banner',m.photo[-1].file_id);await m.answer('Баннер сохранён.')
async def publish_round(rid):
 if await db.fetchval('SELECT 1 FROM gift_publications WHERE round_id=$1',rid):return
 rows=await db.fetch('SELECT prize,count(*) AS n FROM winners WHERE round_id=$1 GROUP BY prize',rid)
 msg=f'🏆 Итоги розыгрыша #{rid}\n'+('\n'.join(f'{x["prize"]}: {x["n"]} победителей' for x in rows) or 'Нет победителей')+'\nПроверьте личный результат в боте.'
 await bot.send_message(CHANNEL,msg,reply_markup=kb([('Проверить результат',f'https://t.me/{username}')]))
 await db.execute('INSERT INTO gift_publications VALUES($1) ON CONFLICT DO NOTHING',rid)
async def extras_job():
 while True:
  try:
   r=await active()
   if r and not r['test']:
    left=(r['ends']-datetime.now(timezone.utc)).total_seconds()
    for h in (24,1):
     if 0<left<=h*3600 and left>(h-1)*3600:
      rows=await db.fetch('SELECT uid FROM players WHERE round_id=$1 AND joined IS NOT NULL AND NOT EXISTS(SELECT 1 FROM gift_reminders g WHERE g.round_id=$1 AND g.uid=players.uid AND g.hours=$2) LIMIT 100',r['id'],h)
      for x in rows:
       await db.execute('INSERT INTO gift_reminders VALUES($1,$2,$3) ON CONFLICT DO NOTHING',r['id'],x['uid'],h)
       await safe_send(x['uid'],f'⏰ До итогов розыгрыша осталось менее {h} ч. Проверьте подписку и билеты в боте.')
       await asyncio.sleep(.08)
   if await setting('auto_publish')=='on':
    rid=await db.fetchval("SELECT max(id) FROM rounds WHERE NOT test AND status='finished' AND NOT EXISTS(SELECT 1 FROM gift_publications WHERE round_id=rounds.id)")
    if rid:await publish_round(rid)
  except Exception:logging.exception('V6 background job')
  await asyncio.sleep(30)

async def main():
 global db,username
 db=await asyncpg.create_pool(os.environ['DATABASE_URL'],min_size=1,max_size=5)
 lock=await asyncpg.connect(os.environ['DATABASE_URL']); key=(await bot.get_me()).id
 stop=asyncio.Event(); loop=asyncio.get_running_loop()
 for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop.set)
 while not stop.is_set():
  if await lock.fetchval('SELECT pg_try_advisory_lock($1::bigint)',key):break
  try:await asyncio.wait_for(stop.wait(),5)
  except asyncio.TimeoutError:pass
 if stop.is_set():await lock.close();await db.close();await bot.session.close();return
 await db.execute(SCHEMA); username=(await bot.get_me()).username
 await bot.delete_webhook(drop_pending_updates=False)
 async def watchdog():
  while True:
   await asyncio.sleep(2)
   try:await lock.fetchval('SELECT 1',timeout=3)
   except Exception:stop.set();return
 tasks=[asyncio.create_task(jobs()),asyncio.create_task(watchdog()),asyncio.create_task(dp.start_polling(bot,handle_signals=False,close_bot_session=False,allowed_updates=dp.resolve_used_update_types())),asyncio.create_task(stop.wait()),asyncio.create_task(extras_job())]
 try:await asyncio.wait([tasks[1],tasks[2],tasks[3]],return_when=asyncio.FIRST_COMPLETED)
 finally:
  for t in tasks:t.cancel()
  await asyncio.gather(*tasks,return_exceptions=True);await bot.session.close();await lock.close();await db.close()
if __name__=='__main__':asyncio.run(main())
