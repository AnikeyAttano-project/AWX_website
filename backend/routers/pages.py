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
    """Страница после неудачной оплаты (в стиле витрины, №42 из правки.txt)."""
    return FAILED_PAGE


HTML_TEMPLATE_STR = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Спасибо за покупку</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
      :root{
        --bg:#F3F2EE;--surface:#FFFFFF;--border:#E7E5E0;--text:#191815;--text-2:#5A564F;--text-3:#6E6A61;
        --accent:#1F5F52;--accent-bg:#EAF1EE;--ok-soft:#EAF1EE;--warn-soft:#FCF5DE;--warn-text:#6B4A00;
      }
      *{box-sizing:border-box;margin:0;padding:0;}
      body{background:var(--bg);color:var(--text);font-family:'Manrope',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
      .card{background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:0 12px 32px rgba(35,32,25,.08);padding:32px;max-width:520px;width:100%;text-align:center;}
      .icon-ok{display:inline-flex;align-items:center;justify-content:center;width:56px;height:56px;border-radius:50%;background:var(--ok-soft);color:var(--accent);margin-bottom:16px;}
      h1{font-size:22px;font-weight:700;margin-bottom:8px;}
      .sub{font-size:14px;color:var(--text-2);margin-bottom:6px;}
      .order-id{font-size:13px;color:var(--text-3);margin-bottom:24px;}
      .btn{display:flex;align-items:center;justify-content:center;width:100%;min-height:44px;padding:11px 18px;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none;border:1px solid transparent;cursor:pointer;transition:all .15s;margin-bottom:12px;}
      .btn-primary{background:var(--accent);color:#fff;}
      .btn-primary:hover{background:#17493F;}
      .btn-secondary{background:var(--surface);color:var(--text);border-color:var(--border);}
      .btn-secondary:hover{border-color:var(--text-3);}
      .notice{background:var(--warn-soft);border-left:3px solid #D9A406;padding:10px 14px;border-radius:0 8px 8px 0;font-size:12.5px;color:var(--warn-text);text-align:left;margin-bottom:14px;line-height:1.5;}
      .link-box{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin:14px 0;word-break:break-all;font-size:13px;color:var(--accent);}
      .qr-wrap{display:flex;justify-content:center;margin:16px 0;}
      .qr-wrap img{width:180px;height:180px;border:1px solid var(--border);border-radius:12px;background:#fff;}
      .status-line{display:flex;align-items:center;justify-content:center;gap:10px;color:var(--text-2);font-size:14px;margin:16px 0;}
      .spinner{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;}
      @keyframes spin{to{transform:rotate(360deg)}}
      .hidden{display:none!important;}
      :focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
      @media (prefers-reduced-motion: reduce){*{animation-duration:.01ms !important;animation-iteration-count:1 !important;}}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon-ok">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none"><path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <h1>Спасибо за покупку!</h1>
        <p class="sub">Оплата получена. Ваш ключ готовится...</p>
        <p class="order-id">Заказ: <strong>{{ order_id }}</strong></p>

        <a href="/account.html" class="btn btn-primary">Перейти в личный кабинет</a>

        <div id="status">
            <div class="status-line">
                <div class="spinner" aria-hidden="true"></div>
                <span id="status-text">Проверяем статус ключа...</span>
            </div>
        </div>

        <div id="result" class="hidden">
            <div class="notice" style="background:var(--ok-soft);border-color:var(--accent);color:var(--accent);font-weight:600;">Ключ уже готов — вот ваша подписка:</div>

            <div class="link-box"><strong>Ваша подписка:</strong><br><code id="sub_url" style="word-break:break-all;"></code></div>

            <div class="qr-wrap">
                <img id="qr_code" src="" alt="QR-код подписки">
            </div>

            <div class="notice">
                <strong>Важно:</strong> Если вы видите только одну ссылку — при импорте в приложение
                (v2rayNG, Hiddify, V2Ray Tun) появятся все серверы.
            </div>
            <div class="notice" style="background:var(--ok-soft);border-color:var(--accent);color:var(--accent);">
                <strong>Инструкция:</strong> Скопируйте ссылку и вставьте в приложение для VPN.
            </div>

            <a href="/account.html" class="btn btn-secondary">Перейти в личный кабинет</a>
        </div>

        <div id="timeout-hint" class="hidden">
            <div class="notice">
                <strong>Ключ ещё готовится.</strong> Не переживайте — он уже в процессе.
                Вы можете забрать его в личном кабинете.
            </div>
            <a href="/account.html" class="btn btn-primary">Забрать ключ в личном кабинете</a>
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


FAILED_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Оплата не прошла</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
      :root{--bg:#F3F2EE;--surface:#FFFFFF;--border:#E7E5E0;--text:#191815;--text-2:#5A564F;--text-3:#6E6A61;--accent:#1F5F52;--err:#C0392B;}
      *{box-sizing:border-box;margin:0;padding:0;}
      body{background:var(--bg);color:var(--text);font-family:'Manrope',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
      .card{background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:0 12px 32px rgba(35,32,25,.08);padding:32px;max-width:420px;width:100%;text-align:center;}
      .icon-err{display:inline-flex;align-items:center;justify-content:center;width:56px;height:56px;border-radius:50%;background:#FBEAEA;color:var(--err);margin-bottom:16px;}
      h1{font-size:20px;font-weight:700;margin-bottom:8px;}
      .sub{font-size:14px;color:var(--text-2);margin-bottom:24px;line-height:1.5;}
      .btn{display:flex;align-items:center;justify-content:center;width:100%;min-height:44px;padding:11px 18px;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none;border:1px solid transparent;cursor:pointer;transition:all .15s;margin-bottom:12px;}
      .btn-primary{background:var(--accent);color:#fff;}
      .btn-primary:hover{background:#17493F;}
      .btn-secondary{background:var(--surface);color:var(--text);border-color:var(--border);}
      .btn-secondary:hover{border-color:var(--text-3);}
      :focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon-err">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><path d="M12 8v5M12 16.5v.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        </div>
        <h1>Оплата не прошла</h1>
        <p class="sub">Платёж не был завершён. Проверьте данные карты или попробуйте снова. Ваши деньги не списаны.</p>
        <a href="/" class="btn btn-primary">Вернуться к тарифам</a>
        <a href="/account.html" class="btn btn-secondary">Перейти в личный кабинет</a>
    </div>
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