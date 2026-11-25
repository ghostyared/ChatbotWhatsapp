from app import create_app

app = create_app()

if __name__ == '__main__':
    # Solo para desarrollo local. En producción se usa gunicorn (ver Dockerfile).
    app.run(host='127.0.0.1', port=5005, debug=False)
