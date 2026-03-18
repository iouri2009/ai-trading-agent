from flask import Flask, request, render_template_string
from agent import run_analysis  # функция из твоего agent.py

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>AI Trading Agent</title>
</head>
<body style="font-family: Arial; text-align: center; padding: 40px;">
    <h1>AI Trading Agent</h1>
    <form method="post">
        <input name="symbol" placeholder="Enter coin (BTCUSDT)" style="padding:10px; width:200px;">
        <button type="submit" style="padding:10px;">Analyze</button>
    </form>
    <pre style="text-align:left; margin-top:30px;">{{ result }}</pre>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def home():
    result = ""
    if request.method == "POST":
        symbol = request.form["symbol"]
        result = run_analysis(symbol)
    return render_template_string(HTML, result=result)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)