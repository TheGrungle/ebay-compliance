from flask import Flask
app = Flask(__name__)

@app.route('/ebay-deletion', methods=['POST', 'GET'])
def deletion():
    return '', 200

if __name__ == '__main__':
    app.run()
