import asyncio, os, logging, secrets, signal
from datetime import datetime, timezone, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton
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
 valid={p['uid']:p for p in rows if await member(p['uid'])}
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
  # Generate a fresh link on every request, retaining the previous link mapping
  # until the new link has been created successfully.
  try:
   invite=await bot.create_chat_invite_link(CHANNEL,creates_join_request=True,expire_date=r['ends']+timedelta(minutes=5))
   link=await db.fetchval('INSERT INTO links VALUES($1,$2,$3) ON CONFLICT(round_id,uid) DO UPDATE SET link=EXCLUDED.link RETURNING link',r['id'],uid,invite.invite_link)
   await db.execute('INSERT INTO join_link_history VALUES($1,$2,$3) ON CONFLICT DO NOTHING',r['id'],uid,link)
   logging.info('Fresh join link created for round %s',r['id'])
  except TelegramAPIError:
   logging.exception('Could not create join link')
   return await m.answer('Не удалось создать ссылку. Попробуйте обновить её.',reply_markup=kb([('Обновить ссылку','check')]))
  return await m.answer('Подайте заявку на вступление в Минский ТГ. Бот автоматически одобрит её и начислит билет. Используйте последнюю ссылку, пока розыгрыш активен.',reply_markup=kb([('Подписаться на канал',link)],[('Обновить ссылку / проверить подписку','check')]))
 await db.execute('UPDATE players SET joined=COALESCE(joined,now()) WHERE round_id=$1 AND uid=$2',r['id'],uid)
 # Only query this participant’s referrals, not the entire audience.
 cutoff=datetime.now(timezone.utc)-timedelta(hours=0 if r['test'] else 24)
 n=await db.fetchval('SELECT count(*) FROM players WHERE round_id=$1 AND ref=$2 AND eligible_new AND joined<=$3',r['id'],uid,cutoff)
 url=f'https://t.me/{username}?start=r{r["id"]}_{uid}'
 await m.answer(('🧪 ТЕСТ: призы виртуальные\n' if r['test'] else '')+f'🎁 Минский ТГ\nБилетов: {weight(n)}\nПодтверждённых друзей: {n}\nВаша ссылка: {url}\nИтоги: {r["ends"].astimezone(timezone(timedelta(hours=3))):%d.%m %H:%M} (Минск)',reply_markup=kb([('Обновить билеты','check')],[('Призы','prizes')]))
@dp.message(CommandStart())
async def start(m:Message):
 r=await active(); uid=m.from_user.id
 if not r or r['ends']<=datetime.now(timezone.utc): return await m.answer('Сейчас нет активного розыгрыша.')
 ref=None; parts=m.text.split(maxsplit=1)
 if len(parts)>1:
  try:
   rid,ref=map(int,parts[1][1:].split('_')); assert parts[1].startswith('r') and rid==r['id'] and ref!=uid
   if not await db.fetchval('SELECT 1 FROM players WHERE round_id=$1 AND uid=$2 AND joined IS NOT NULL',rid,ref): ref=None
  except (ValueError,AssertionError): ref=None
 existing=await member(uid)
 await db.execute('INSERT INTO players(round_id,uid,ref,eligible_new,joined) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',r['id'],uid,ref,not existing,datetime.now(timezone.utc) if existing else None)
 await show(m,uid)
@dp.callback_query(F.data=='check')
async def check(c:CallbackQuery):
 await c.answer(); await show(c.message,c.from_user.id)
@dp.callback_query(F.data=='prizes')
async def prizes(c:CallbackQuery):
 await c.answer(); r=await active()
 await c.message.answer('Тест: 3 виртуальных приза.' if r and r['test'] else '1 Яндекс Станция Мини\n10 сертификатов WB по 50 BYN\n50 подарков Telegram')
class PrivateReply:
 def __init__(self,uid): self.uid=uid
 async def answer(self,text,**kwargs): return await bot.send_message(self.uid,text,**kwargs)
@dp.chat_join_request()
async def join(j:ChatJoinRequest):
 if j.chat.id!=CHANNEL or not j.invite_link: return
 p=await db.fetchrow("SELECT l.*,r.test,r.ends FROM (SELECT round_id,uid,link FROM links UNION SELECT round_id,uid,link FROM join_link_history) l JOIN rounds r ON r.id=l.round_id WHERE l.link=$1 AND r.status='active'",j.invite_link.invite_link)
 if not p or p['uid']!=j.from_user.id or p['ends']<=datetime.now(timezone.utc): return
 await bot.approve_chat_join_request(CHANNEL,j.from_user.id)
 await db.execute('UPDATE players SET joined=COALESCE(joined,now()) WHERE round_id=$1 AND uid=$2',p['round_id'],p['uid'])
 try:
  await bot.send_message(p['uid'],'✅ Подписка подтверждена! Ваш билет начислен.')
  await show(PrivateReply(p['uid']),p['uid'])
 except TelegramAPIError: logging.exception('join notification')
 ref=await db.fetchval('SELECT ref FROM players WHERE round_id=$1 AND uid=$2 AND eligible_new',p['round_id'],p['uid'])
 if ref and p['test']:
  try:
   await bot.send_message(ref,'🎉 Ваш друг вступил в канал! Вам начислен дополнительный билет, если вы остаётесь подписчиком.')
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
 await m.answer('Управление: '+(f'активен #{r["id"]}' if r else 'нет розыгрыша'),reply_markup=kb([('🧪 Тест 30 минут','test')],[('Статистика','stats'),('Завершить тест','finish')],[('Удалить тесты','purge')],[('Реальный розыгрыш','live')]))
@dp.message(Command('diagnostics'))
async def diagnostics(m:Message):
 if m.from_user.id!=ADMIN:return
 try:
  me=await bot.get_me(); chat=await bot.get_chat(CHANNEL); rights=await bot.get_chat_member(CHANNEL,me.id); await db.fetchval('SELECT 1')
  await m.answer(f'Telegram и PostgreSQL: OK\nКанал: {chat.title}\nСтатус: {rights.status}\nПриглашения: {getattr(rights,"can_invite_users",False)}\nБлокировка запуска: активна')
 except Exception as e: await m.answer(f'Ошибка диагностики: {type(e).__name__} {str(e)[:300]}')
async def finish(r):
 refs=await counts(r); pool=dict(refs); result=[]
 prizes=['Тестовый приз '+str(i) for i in range(1,4)] if r['test'] else ['Яндекс Станция Мини']+['Сертификат WB 50 BYN']*10+['Подарок Telegram']*50
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
@dp.callback_query(F.data.in_({'test','stats','finish','purge','live','live_yes'}))
async def control(c:CallbackQuery):
 if c.from_user.id!=ADMIN:return await c.answer('Нет доступа',show_alert=True)
 await c.answer(); r=await active(); a=c.data
 if a=='live':return await c.message.answer('Реальный режим пока заблокирован: сначала проверяем тест и условия акции.')
 if a=='test':
  if r:return await c.message.answer('Уже есть активный розыгрыш.')
  await db.execute("INSERT INTO rounds(test,ends) VALUES(true,now()+interval '30 minutes')")
  return await c.message.answer('Тест запущен. /start')
 if a=='stats':
  if not r:return await c.message.answer('Нет активного розыгрыша.')
  n=await db.fetchval('SELECT count(*) FROM players WHERE round_id=$1',r['id']); return await c.message.answer(f'Участников: {n}')
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
    if not p['test']:continue
    # Notification only, never a paid gift.
    try:await bot.send_message(p['uid'],f'🧪 Тест: вы выиграли {p["prize"]}. Реальный подарок не отправляется.')
    except TelegramAPIError:logging.exception('notification failed');continue
    await db.execute('UPDATE winners SET notified=true WHERE round_id=$1 AND uid=$2',p['round_id'],p['uid'])
  except Exception:logging.exception('scheduler')
  await asyncio.sleep(5)
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
 tasks=[asyncio.create_task(jobs()),asyncio.create_task(watchdog()),asyncio.create_task(dp.start_polling(bot,handle_signals=False,close_bot_session=False,allowed_updates=dp.resolve_used_update_types())),asyncio.create_task(stop.wait())]
 try:await asyncio.wait([tasks[1],tasks[2],tasks[3]],return_when=asyncio.FIRST_COMPLETED)
 finally:
  for t in tasks:t.cancel()
  await asyncio.gather(*tasks,return_exceptions=True);await bot.session.close();await lock.close();await db.close()
if __name__=='__main__':asyncio.run(main())
