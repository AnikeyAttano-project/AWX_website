"""HTML-страницы после оплаты: /payment/success, /payment/failed."""
import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["pages"])

@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success(order_id: str, token: str = ""):
    """Страница после успешной оплаты — показывает sub-ссылку и QR."""
    # Передаём capability-токен в шаблон для polling
    return HTML_TEMPLATE.render(order_id=order_id, capability_token=token)


@router.get("/payment/failed", response_class=HTMLResponse)
async def payment_failed(order_id: str):
    """Страница после неудачной оплаты."""
    return "<html><body><h2>Оплата не удалась</h2><p>Попробуйте снова.</p></body></html>"


HTML_TEMPLATE_STR = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Спасибо за покупку!</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="bg-white rounded-lg shadow-lg p-8 max-w-2xl w-full">
        <h1 class="text-3xl font-bold text-green-600 mb-4">✅ Спасибо за покупку!</h1>
        <p class="text-gray-700 mb-2">Оплата получена. Ваш ключ готовится...</p>
        <p class="text-gray-500 text-sm mb-6">Заказ: <strong>{{ order_id }}</strong></p>

        <!-- Кнопка в ЛК — доступна СРАЗУ, не ждём загрузки ключа -->
        <a href="/account.html" class="block w-full text-center bg-green-600 text-white font-semibold py-3 px-6 rounded-lg hover:bg-green-700 transition mb-6 text-lg">
            🔑 Перейти в личный кабинет
        </a>

        <div id="status" class="mb-6">
            <div class="flex items-center gap-3 text-gray-500 text-sm">
                <svg class="animate-spin h-5 w-5 text-green-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
                <span id="status-text">Проверяем статус ключа...</span>
            </div>
        </div>

        <div id="result" class="hidden">
            <div class="bg-green-50 border-l-4 border-green-500 p-4 mb-4">
                <p class="text-sm text-green-700 font-semibold">🎉 Ключ уже готов! Вот ваша подписка:</p>
            </div>

            <div class="bg-blue-50 border-l-4 border-blue-500 p-4 mb-4">
                <p class="text-sm text-blue-700">
                    <strong>Ваша подписка:</strong><br>
                    <code id="sub_url" class="bg-white px-2 py-1 rounded text-xs break-all"></code>
                </p>
            </div>

            <div class="flex justify-center mb-4">
                <img id="qr_code" src="" alt="QR Code" class="border-4 border-gray-200 rounded">
            </div>

            <div class="bg-yellow-50 border-l-4 border-yellow-500 p-4 mb-4">
                <p class="text-sm text-yellow-700">
                    <strong>⚠️ Важно:</strong> Если вы видите только одну ссылку — при импорте в приложение
                    (v2rayNG, Hiddify, V2Ray Tun) появятся все 8 серверов.
                </p>
            </div>

            <div class="bg-green-50 border-l-4 border-green-500 p-4 mb-4">
                <p class="text-sm text-green-700">
                    <strong>📱 Инструкция:</strong> Скопируйте ссылку и вставьте в приложение для VPN.
                </p>
            </div>

            <a href="/account.html" class="block w-full text-center bg-gray-600 text-white font-medium py-2 px-4 rounded hover:bg-gray-700 transition">
                Перейти в личный кабинет
            </a>
        </div>

        <!-- Если ключ НЕ загрузился — показываем ссылку на ЛК -->
        <div id="timeout-hint" class="hidden">
            <div class="bg-amber-50 border-l-4 border-amber-400 p-4 mb-4">
                <p class="text-sm text-amber-700">
                    <strong>Ключ ещё готовится.</strong> Не переживайте — он уже в процессе.
                    Вы можете забрать его в личном кабинете.
                </p>
            </div>
            <a href="/account.html" class="block w-full text-center bg-green-600 text-white font-semibold py-3 px-6 rounded-lg hover:bg-green-700 transition">
                🔑 Забрать ключ в личном кабинете
            </a>
        </div>
    </div>

    <script>
        const orderId = "{{ order_id }}";
        const capabilityToken = "{{ capability_token }}";
        let attempts = 0;
        const maxAttempts = 15; // 30 секунд — потом предлагаем ЛК

        function showTimeoutHint() {
            const s = document.getElementById("status");
            const h = document.getElementById("timeout-hint");
            if (s) s.classList.add("hidden");
            if (h) h.classList.remove("hidden");
        }

        async function checkStatus() {
            attempts++;
            try {
                const resp = await fetch(`/api/order/${orderId}/status?token=${capabilityToken}`);
                const data = await resp.json();

                if (data.sub_url) {
                    // Ключ готов — показываем сразу
                    document.getElementById("sub_url").textContent = data.sub_url;
                    document.getElementById("qr_code").src = "data:image/png;base64," + data.qr_base64;
                    document.getElementById("status").classList.add("hidden");
                    document.getElementById("result").classList.remove("hidden");
                } else if (data.ready) {
                    // Ключ есть, но нет авторизации — тоже предлагаем ЛК
                    showTimeoutHint();
                } else if (attempts < maxAttempts) {
                    // Ещё ждём — обновляем текст
                    const dots = '.'.repeat((attempts % 3) + 1);
                    document.getElementById("status-text").textContent = `Проверяем статус ключа${dots}`;
                    setTimeout(checkStatus, 2000);
                } else {
                    // 30 секунд прошли — предлагаем ЛК
                    showTimeoutHint();
                }
            } catch (e) {
                console.error(e);
                if (attempts < maxAttempts) {
                    setTimeout(checkStatus, 2000);
                } else {
                    showTimeoutHint();
                }
            }
        }

        checkStatus();
    </script>
</body>
</html>
"""


class HTMLTemplate:
    def render(self, **kwargs):
        html = HTML_TEMPLATE_STR
        for key, value in kwargs.items():
            # Escape for JavaScript string literal (produces a safe JS string with quotes)
            escaped = json.dumps(str(value))
            # Дополнительная защита для inline <script>:
            # JSON не экранирует <, >, & — заменяем на \uXXXX, чтобы злоумышленник
            # не смог выйти из строки через "</script>".
            escaped = (
                escaped.replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            html = html.replace(f"{{{{ {key} }}}}", escaped)
        return html


HTML_TEMPLATE = HTMLTemplate()