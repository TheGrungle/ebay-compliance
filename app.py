import hashlib
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFICATION_TOKEN = "qawfjoewjfoiewfsadfjjwqoifjewoifjoiwjfluhojflanfmdnugjwoiqjfnewfow"
ENDPOINT = "https://ebay-compliance-5902.onrender.com/ebay-deletion"

@app.route('/ebay-deletion', methods=['GET', 'POST'])
def deletion():
    challenge = request.args.get('challenge_code')
    if challenge:
        m = hashlib.sha256()
        m.update(challenge.encode('utf-8'))
        m.update(VERIFICATION_TOKEN.encode('utf-8'))
        m.update(ENDPOINT.encode('utf-8'))
        return jsonify({"challengeResponse": m.hexdigest()}), 200
    return '', 200

if __name__ == '__main__':
    app.run()
