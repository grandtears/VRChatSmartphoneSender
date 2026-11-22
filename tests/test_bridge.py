import json
import socket
import subprocess
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from app import Bridge, handler_for, load_presets, osc_packet, vrchat_running


class Tests(unittest.TestCase):
    """HTTP、OSC、クリップボード、画面構成の主要な契約を確認する。"""

    def test_phone_page_destination_controls(self):
        """スマートフォン画面に新しい送信先UIと判定処理が含まれること。"""
        bridge = Bridge(lambda text: None)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = HTTPConnection(*server.server_address, timeout=3)
            conn.request('GET', '/')
            response = conn.getresponse()
            page = response.read().decode('utf-8')
            conn.close()

            self.assertEqual(response.status, 200)
            self.assertIn('<title>VRCSmartphoneSender</title>', page)
            self.assertIn('id="to-chat" type="checkbox" checked', page)
            self.assertIn('id="to-clip" type="checkbox"', page)
            self.assertIn('<button id="send">送信</button>', page)
            self.assertIn("return 'both'", page)
            self.assertIn("return 'chat'", page)
            self.assertIn("return 'clipboard'", page)
            self.assertNotIn('data-mode=', page)

            conn = HTTPConnection(*server.server_address, timeout=3)
            conn.request('GET', '/presets')
            response = conn.getresponse()
            presets = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertIn('こんにちは！', presets)
        finally:
            server.shutdown()
            server.server_close()

    def test_load_presets_from_external_file(self):
        """UTF-8テキストの各行を定型文として読み、空行を除外すること。"""
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'presets.txt').write_text(
                '  こんにちは  \n\nありがとう\n',
                encoding='utf-8',
            )
            with patch('app.application_directory', return_value=path):
                self.assertEqual(load_presets(), ['こんにちは', 'ありがとう'])

            (path / 'presets.txt').unlink()
            with patch('app.application_directory', return_value=path):
                self.assertEqual(load_presets(), [])

    def test_destination_modes(self):
        """送信モードごとに、意図した送信先だけが呼ばれること。"""
        copied = []
        clipboard_bridge = Bridge(copied.append)
        with patch('app.socket.socket') as sock:
            result = clipboard_bridge.send(
                {'text': 'コピーのみ', 'mode': 'clipboard'}
            )
            sock.assert_not_called()
        self.assertEqual(copied, ['コピーのみ'])
        self.assertIn('コピーしました', result['message'])

        chat_bridge = Bridge(copied.append)
        with patch('app.socket.socket') as socket_factory:
            udp = socket_factory.return_value.__enter__.return_value
            result = chat_bridge.send({'text': 'チャットのみ', 'mode': 'chat'})
            udp.sendto.assert_called_once_with(
                osc_packet('チャットのみ'), ('127.0.0.1', 9000)
            )
        self.assertEqual(copied, ['コピーのみ'])
        self.assertIn('OSC送信しました', result['message'])

        with self.assertRaises(ValueError):
            Bridge(copied.append).send({'text': '送信', 'mode': ''})

    def test_osc_japanese(self):
        """日本語や改行を含む文章が、正しいOSCパケットになること。"""
        packet = osc_packet('こんにちは\n世界')
        self.assertEqual(len(packet) % 4, 0)
        self.assertTrue(packet.startswith(b'/chatbox/input\0\0'))
        self.assertIn(b',sTF\0', packet)
        self.assertIn('こんにちは\n世界'.encode(), packet)

    def test_clipboard_preserves_long_text(self):
        """長文はクリップボードのみ許可し、VRChat向けでは拒否すること。"""
        copied = []
        bridge = Bridge(copied.append)
        text = '日本語\n' * 100
        bridge.send({'text': text, 'mode': 'clipboard'})
        self.assertEqual(copied, [text])
        with self.assertRaises(ValueError):
            bridge.send({'text': text, 'mode': 'both'})
        self.assertEqual(copied, [text])

    def test_udp_and_cooldown(self):
        """OSCがUDPで送られ、短時間の連続送信が制限されること。"""
        copied = []
        bridge = Bridge(copied.append)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.settimeout(2)
            target = listener.getsockname()
            real_socket = socket.socket

            # 実際の9000番ではなくテスト用UDPポートへ転送し、
            # VRChatを起動せずにパケット内容を検証する。
            class Redirect:
                def __enter__(self):
                    self.sock = real_socket(socket.AF_INET, socket.SOCK_DGRAM)
                    return self
                def sendto(self, data, address):
                    assert address == ('127.0.0.1', 9000)
                    return self.sock.sendto(data, target)
                def __exit__(self, *args):
                    self.sock.close()
            with patch('app.socket.socket', return_value=Redirect()):
                bridge.send({'text': '日本語', 'mode': 'both'})
            self.assertEqual(listener.recv(2048), osc_packet('日本語'))
            self.assertEqual(copied, ['日本語'])
            with self.assertRaises(ValueError):
                bridge.send({'text': '再送', 'mode': 'both'})
            self.assertEqual(copied, ['日本語'])

    def test_http_auth_origin_and_copy(self):
        """接続キー・Origin・JSON形式がHTTP APIで検証されること。"""
        copied = []
        bridge = Bridge(copied.append)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def request(key, origin=None, body=None):
            conn = HTTPConnection(*server.server_address, timeout=3)
            headers = {'X-Pair-Key': key, 'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            conn.request('POST', '/send', json.dumps(body or {'text': 'コピー確認', 'mode': 'clipboard'}), headers)
            response = conn.getresponse()
            code = response.status
            response.read()
            conn.close()
            return code
        try:
            self.assertEqual(request('wrong'), 403)
            self.assertEqual(request(bridge.token, 'http://evil.example'), 403)
            self.assertEqual(copied, [])
            self.assertEqual(request(bridge.token), 200)
            self.assertEqual(copied, ['コピー確認'])
            self.assertEqual(request(bridge.token, body=['invalid']), 400)
        finally:
            server.shutdown()
            server.server_close()

    def test_vrchat_running(self):
        """tasklistの結果を起動中・未起動・不明へ正しく変換すること。"""
        with patch('app.os.name', 'posix'):
            self.assertIsNone(vrchat_running())
        with patch('app.os.name', 'nt'), patch('app.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '"VRChat.exe","1234","Console","1","1 K"\n')
            self.assertTrue(vrchat_running())
            run.return_value = subprocess.CompletedProcess([], 0, 'INFO: No tasks are running which match the specified criteria.\n')
            self.assertFalse(vrchat_running())
            run.side_effect = OSError('tasklist')
            self.assertIsNone(vrchat_running())

    def test_copy_failure_stops_chat(self):
        """両方送信でコピーに失敗した場合、OSCを送らないこと。"""
        def fail(text):
            raise RuntimeError('clipboard busy')
        bridge = Bridge(fail)
        with patch('app.socket.socket') as sock:
            with self.assertRaises(RuntimeError):
                bridge.send({'text': 'hello', 'mode': 'both'})
            sock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
