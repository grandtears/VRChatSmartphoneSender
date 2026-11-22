"""スマートフォンからVRChatのチャットボックスとPCへ文章を送るアプリ。

PC上でローカルHTTPサーバーとGUIを起動し、同じLAN内のスマートフォンへ
入力画面を提供します。VRChatへの送信にはOSC（UDP 9000番）を使用します。
"""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PORT = 8765


def application_directory():
    """ユーザーが編集できる外部ファイルを置くアプリのフォルダーを返す。"""
    if getattr(sys, 'frozen', False):
        # exe版では一時展開先ではなく、exe本体が置かれている場所を使う。
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_presets():
    """presets.txtを読み込み、空行を除いた定型文の一覧を返す。"""
    try:
        lines = (application_directory() / 'presets.txt').read_text(
            encoding='utf-8'
        ).splitlines()
    except (OSError, UnicodeError):
        # ファイルがない・UTF-8でない場合も、アプリ本体は停止させない。
        return []

    # 誤って巨大なファイルを指定した場合に備え、先頭100件までを公開する。
    return [line.strip() for line in lines if line.strip()][:100]


def osc_string(value):
    """文字列をOSC仕様の文字列（4バイト境界、NULL終端）へ変換する。"""
    raw = value.encode('utf-8') + b'\0'
    return raw + b'\0' * (-len(raw) % 4)


def osc_packet(text, sound=False):
    """VRChatの /chatbox/input に送るOSCパケットを組み立てる。"""
    return osc_string('/chatbox/input') + osc_string(',sT' + ('T' if sound else 'F')) + osc_string(text)


class Bridge:
    """Web画面から届いた要求を、クリップボードまたはOSCへ橋渡しする。"""

    def __init__(self, copy_text):
        # 接続キーは起動のたびに生成し、過去のQRコードを再利用できないようにする。
        self.token = secrets.token_urlsafe(24)
        self.copy_text = copy_text
        self.last_chat = 0
        # HTTPサーバーは複数スレッドで動くため、送信順序と送信間隔を保護する。
        self.lock = threading.Lock()

    def send(self, data):
        """入力値を検証し、指定された送信先へ文章を送る。"""
        text = data.get('text')
        mode = data.get('mode')

        # OSC文字列にはNULL文字を含められない。極端に大きな要求もここで拒否する。
        if not isinstance(text, str) or not text.strip():
            raise ValueError('文章を入力してください。')
        if '\0' in text or len(text.encode('utf-8')) > 60000:
            raise ValueError('文章が長すぎるか、使用できない文字が含まれています。')
        if mode not in ('chat', 'clipboard', 'both'):
            raise ValueError('送信先が不正です。')
        # VRChatはUTF-16コード単位で最大144文字、最大9行。
        # 絵文字などは2コード単位になるため、PythonのlenではなくUTF-16で数える。
        if mode != 'clipboard' and (len(text.encode('utf-16-le')) // 2 > 144 or len(text.split('\n')) > 9):
            raise ValueError('VRChatは144文字・9行以内にしてください。折り返しも表示行数に含まれます。')
        with self.lock:
            # time.monotonic()なら、PCの時刻が変更されても待機時間の判定が乱れない。
            if mode != 'clipboard' and time.monotonic() - self.last_chat < 1.5:
                raise ValueError('少し待ってから送信してください。')
            # 両方へ送る場合はコピーを先に行う。
            # コピー失敗時にチャットだけが送信される、分かりにくい状態を防ぐため。
            if mode in ('clipboard', 'both'):
                self.copy_text(text)
            if mode in ('chat', 'both'):
                try:
                    # VRChatの標準OSC受信先は、同じPCのUDP 9000番。
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                        sock.sendto(osc_packet(text, data.get('sound') is True), ('127.0.0.1', 9000))
                except OSError as exc:
                    raise ValueError('OSC送信に失敗しました。コピー済みの場合は保持されています。') from exc
                self.last_chat = time.monotonic()
        return {'ok': True, 'message': 'PCへコピーしました。' if mode == 'clipboard' else ('PCへコピー・OSC送信しました。' if mode == 'both' else 'OSC送信しました。') + ' VRChat上の表示はゲーム内で確認してください。'}


def handler_for(bridge):
    """Bridgeを共有するHTTPリクエストハンドラークラスを作成する。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # 標準のアクセスログには接続情報が含まれるため、プライバシー保護のため出力しない。
            pass

        def reply(self, status, body, kind='application/json; charset=utf-8'):
            """キャッシュ禁止などの安全用ヘッダーを付けて応答する。"""
            raw = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/':
                return self.reply(
                    200,
                    Path(__file__).with_name('phone.html').read_text(encoding='utf-8'),
                    'text/html; charset=utf-8',
                )
            if self.path == '/presets':
                # ファイルそのものではなくJSONだけを返し、任意ファイルの閲覧を防ぐ。
                return self.reply(
                    200,
                    json.dumps(load_presets(), ensure_ascii=False),
                )
            # スマートフォン用画面と定型文API以外のパスは公開しない。
            return self.reply(404, '{}')

        def do_POST(self):
            # 文章送信APIは /send のみ。
            if self.path != '/send':
                return self.reply(404, '{}')
            # timing attackを避けるため、通常の == ではなくcompare_digestで接続キーを比較する。
            if not secrets.compare_digest(self.headers.get('X-Pair-Key', ''), bridge.token):
                return self.reply(403, json.dumps({'error': '接続キーが違います。PCのQRコードを読み直してください。'}, ensure_ascii=False))
            # 別サイトから勝手に送信されることを防ぐ。同一ホストからの要求だけ許可する。
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(403, '{}')
            try:
                # 巨大なリクエストを読み込んでメモリーを消費しないよう、先にサイズを制限する。
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ValueError('データサイズが不正です。')
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('データ形式が不正です。')
                result = bridge.send(data)
                self.reply(200, json.dumps(result, ensure_ascii=False))
            except (ValueError, OSError, RuntimeError) as exc:
                self.reply(400, json.dumps({'error': str(exc)}, ensure_ascii=False))
    return Handler


def local_ips():
    """スマートフォンから接続できる可能性があるPCのIPアドレスを列挙する。"""
    addresses = {'127.0.0.1'}
    try:
        # ホスト名から得られるアドレスに加え、通常の通信に使う経路のアドレスも調べる。
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # 実際に外部へパケットは送らず、OSが選ぶ送信元アドレスだけを取得する。
            sock.connect(('192.0.2.1', 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    # 127.0.0.1はスマートフォンから使えないため、候補の最後へ回す。
    return sorted(addresses, key=lambda x: x == '127.0.0.1')


def vrchat_running():
    """Windowsのプロセス一覧からVRChatの起動状態を返す。不明時はNone。"""
    if os.name != 'nt':
        return None
    try:
        completed = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq VRChat.exe', '/NH', '/FO', 'CSV'],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return 'VRChat.exe' in completed.stdout


def main():
    """GUI・HTTPサーバー・VRChat監視を起動し、終了まで管理する。"""
    try:
        import customtkinter as ctk
    except ImportError:
        print('GUIの表示には setup.bat を実行してください。')
        input('Enterで終了')
        return

    # 白と水色を基調にした共通カラーパレット。
    bg = '#F4FAFD'
    card = '#FFFFFF'
    accent = '#3C9FCF'
    accent_hover = '#2D86B3'
    soft = '#E6F4FB'
    text = '#173748'
    muted = '#5E7C8C'
    line = '#D5E8F2'
    ok = '#1C9A62'
    ok_bg = '#E6F7EF'
    off = '#C47B2B'
    off_bg = '#FFF6EA'
    font = 'Yu Gothic UI'

    ctk.set_appearance_mode('light')
    ctk.set_default_color_theme('blue')
    root = ctk.CTk()
    root.title('VRCSmartphoneSender v0.1 — VRChat / PC Clipboard')
    root.geometry('500x800')
    root.minsize(460, 740)
    root.configure(fg_color=bg)

    # HTTP処理は別スレッドで動くが、Tkinterはメインスレッドからしか安全に操作できない。
    # そのため、クリップボード操作をこのキュー経由でメインスレッドへ渡す。
    tasks = queue.Queue()

    def copy_text(text):
        """HTTPスレッドからコピーを依頼し、GUI側の完了を待つ。"""
        done = threading.Event()
        task = {'text': text, 'done': done, 'error': None, 'cancelled': False}
        tasks.put(task)
        if not done.wait(4):
            task['cancelled'] = True
            raise RuntimeError('PCのコピー処理がタイムアウトしました。')
        if task['error']:
            raise RuntimeError('クリップボードを開けませんでした。再送信してください。')

    bridge = Bridge(copy_text)
    # 0.0.0.0で待ち受け、同じLANにいるスマートフォンから接続可能にする。
    server = ThreadingHTTPServer(('0.0.0.0', PORT), handler_for(bridge))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def tick():
        """キューに届いたGUI操作を定期的にメインスレッドで処理する。"""
        try:
            while True:
                task = tasks.get_nowait()
                try:
                    if not task['cancelled']:
                        root.clipboard_clear()
                        root.clipboard_append(task['text'])
                        status.set('PCのクリップボードにコピーしました。')
                # Tkのクリップボードは、他アプリが使用中だと失敗することがある。
                except Exception as exc:
                    task['error'] = str(exc)
                finally:
                    task['done'].set()
        except queue.Empty:
            pass
        root.after(40, tick)

    status = ctk.StringVar(value='受信待機中 — 終了するにはこのウィンドウを閉じます。')
    # バックグラウンド監視スレッドを安全に止めるための共有フラグ。
    alive = True

    # ----- ウィンドウ上部とステータスバー -----
    ctk.CTkFrame(root, height=6, fg_color=accent, corner_radius=0).pack(fill='x')
    bar = ctk.CTkFrame(root, fg_color=card, corner_radius=0, height=46, border_width=1, border_color=line)
    bar.pack(fill='x', side='bottom')
    bar.pack_propagate(False)
    ctk.CTkLabel(bar, textvariable=status, anchor='w', text_color=text, font=ctk.CTkFont(family=font, size=12)).pack(fill='both', expand=True, padx=18)
    body = ctk.CTkFrame(root, fg_color=bg, corner_radius=0)
    body.pack(fill='both', expand=True, padx=8, pady=(8, 0))

    ctk.CTkLabel(body, text='VRC SMARTPHONE SENDER', anchor='w', text_color=accent, font=ctk.CTkFont(family=font, size=13, weight='bold')).pack(fill='x', padx=14, pady=(8, 0))
    ctk.CTkLabel(body, text='スマホからVRChatへ送信。', anchor='w', text_color=text, font=ctk.CTkFont(family=font, size=24, weight='bold')).pack(fill='x', padx=14, pady=(2, 2))
    ctk.CTkLabel(body, text='同じWi-FiのスマホでQRコードを読み取ってください。', anchor='w', text_color=muted, font=ctk.CTkFont(family=font, size=13)).pack(fill='x', padx=14)

    # ----- VRChat起動状態カード -----
    vr = ctk.CTkFrame(body, fg_color=card, corner_radius=18, border_width=1, border_color=line)
    vr.pack(fill='x', padx=14, pady=(16, 12))
    vr.grid_columnconfigure(1, weight=1)
    mark = ctk.CTkLabel(vr, text='●', width=28, text_color=accent, font=ctk.CTkFont(family=font, size=18))
    mark.grid(row=0, column=0, rowspan=2, padx=(16, 2), pady=14)
    ctk.CTkLabel(vr, text='VRChat', anchor='w', text_color=text, font=ctk.CTkFont(family=font, size=16, weight='bold')).grid(row=0, column=1, sticky='sw', pady=(14, 0))
    detail = ctk.CTkLabel(vr, text='起動状態を確認しています。', anchor='w', justify='left', wraplength=280, text_color=muted, font=ctk.CTkFont(family=font, size=12))
    detail.grid(row=1, column=1, sticky='nw', pady=(0, 14))
    pill = ctk.CTkLabel(vr, text='確認中', width=88, height=30, corner_radius=15, fg_color=soft, text_color=accent, font=ctk.CTkFont(family=font, size=13, weight='bold'))
    pill.grid(row=0, column=2, rowspan=2, padx=(8, 16))

    def apply_vrchat(running):
        """検出結果に合わせて、表示文とステータス色を更新する。"""
        if not alive:
            return
        if running is True:
            mark.configure(text_color=ok)
            pill.configure(text='起動中', fg_color=ok_bg, text_color=ok)
            detail.configure(text='VRChat.exe を検出しました。OSCの送信先は 127.0.0.1:9000 です。')
        elif running is False:
            mark.configure(text_color=off)
            pill.configure(text='未起動', fg_color=off_bg, text_color=off)
            detail.configure(text='VRChatが起動していません。起動後にOSCを有効にしてください。')
        else:
            mark.configure(text_color=muted)
            pill.configure(text='不明', fg_color=soft, text_color=accent)
            detail.configure(text='起動状態を確認できませんでした。')

    def watch_vrchat():
        """GUIを止めないよう、別スレッドで2秒ごとにVRChatを確認する。"""
        while alive:
            running = vrchat_running()
            if not alive:
                return
            try:
                # Tkウィジェットの更新はroot.afterでメインスレッドへ戻す。
                root.after(0, lambda state=running: apply_vrchat(state))
            except RuntimeError:
                return
            time.sleep(2)

    # ----- スマートフォン接続カード -----
    connect = ctk.CTkFrame(body, fg_color=card, corner_radius=18, border_width=1, border_color=line)
    connect.pack(fill='x', padx=14, pady=(0, 12))
    ctk.CTkLabel(connect, text='接続先', anchor='w', text_color=muted, font=ctk.CTkFont(family=font, size=13, weight='bold')).pack(fill='x', padx=18, pady=(14, 4))
    ips = local_ips()
    ip = ctk.StringVar(value=ips[0])
    url = ctk.StringVar()
    qr_label = ctk.CTkLabel(connect, text='', fg_color=card)

    def refresh(_value=None):
        """選択中のIPから接続URLとQRコードを作り直す。"""
        # 接続キーはURLのフラグメント（#以降）へ入れる。
        # フラグメントはHTTPリクエストに含まれないため、アクセスログへ残りにくい。
        url.set(f'http://{ip.get()}:{PORT}/#' + bridge.token)
        try:
            import qrcode
            from PIL import Image
            img = qrcode.make(url.get()).resize((220, 220), getattr(Image, 'Resampling', Image).NEAREST).convert('RGB')
            # CTkImageへの参照をqr_labelへ保持し、ガベージコレクションによる画像消失を防ぐ。
            photo = ctk.CTkImage(light_image=img, dark_image=img, size=(220, 220))
            qr_label.photo = photo
            qr_label.configure(image=photo, text='')
        except ImportError:
            qr_label.configure(text='QR表示には setup.bat を実行してください。\n上のURLをスマホへ入力することもできます。')

    def copy_url():
        """接続URLをPCのクリップボードへコピーする。"""
        root.clipboard_clear()
        root.clipboard_append(url.get())
        status.set('接続URLをコピーしました。共有しないでください。')

    ctk.CTkComboBox(
        connect, values=ips, variable=ip, command=refresh, state='readonly', height=38, corner_radius=12,
        fg_color=bg, border_color=line, button_color=accent, button_hover_color=accent_hover,
        dropdown_fg_color=card, dropdown_hover_color=soft, dropdown_text_color=text, text_color=text,
        font=ctk.CTkFont(family=font, size=13), dropdown_font=ctk.CTkFont(family=font, size=13),
    ).pack(fill='x', padx=16)
    qr_label.pack(pady=(14, 4))
    url_row = ctk.CTkFrame(connect, fg_color=bg, corner_radius=12)
    url_row.pack(fill='x', padx=16, pady=(4, 8))
    url_row.grid_columnconfigure(0, weight=1)
    url_label = ctk.CTkLabel(url_row, textvariable=url, anchor='w', justify='left', text_color=text, wraplength=280, font=ctk.CTkFont(family=font, size=12))
    url_label.grid(row=0, column=0, sticky='ew', padx=(12, 8), pady=8)
    ctk.CTkButton(
        url_row, text='コピー', width=84, height=32, command=copy_url, corner_radius=10,
        fg_color=accent, hover_color=accent_hover, text_color='#FFFFFF', font=ctk.CTkFont(family=font, size=13, weight='bold'),
    ).grid(row=0, column=1, padx=(0, 8), pady=8)
    ctk.CTkButton(
        connect, text='PCで入力画面を開く', command=lambda: webbrowser.open(url.get()), height=42, corner_radius=12,
        fg_color=accent, hover_color=accent_hover, text_color='#FFFFFF', font=ctk.CTkFont(family=font, size=14, weight='bold'),
    ).pack(fill='x', padx=16, pady=(4, 16))
    refresh()

    ctk.CTkLabel(
        body,
        text='VRChatのOSCを有効にしてください。送信先は 127.0.0.1:9000 です。\nWindowsの通信許可は「プライベートネットワーク」を選んでください。\n接続キーは起動ごとに変わります。URLとQRコードは共有しないでください。',
        anchor='w', justify='left', text_color=muted, font=ctk.CTkFont(family=font, size=12),
    ).pack(fill='x', padx=18, pady=(0, 16))

    def on_close():
        """監視を止めてからGUIを閉じる。HTTPサーバーはmainloop終了後に停止する。"""
        nonlocal alive
        alive = False
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    threading.Thread(target=watch_vrchat, daemon=True).start()
    root.after(40, tick)
    root.mainloop()

    # ウィンドウを閉じた後に待受ソケットも確実に解放する。
    server.shutdown()
    server.server_close()


if __name__ == '__main__':
    try:
        main()
    except OSError as exc:
        print('起動できません。二重起動・ポート8765の使用状況を確認してください。', exc)
        input('Enterで終了')
