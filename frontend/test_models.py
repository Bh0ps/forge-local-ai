"""Live model connection check; regression suite is test_app.py."""
import json
import urllib.request
if __name__ == "__main__":
    request=urllib.request.Request("http://127.0.0.1:8081/api/models",data=b"{}",headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=10) as response:
        data=json.load(response)
    print("Installed models:",len(data["models"]))
