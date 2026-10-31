import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from core import DEFAULTS, Engine, Store, TZ, fetch_offers, rank_offers, shopee_query, validate_settings
from app import create_app


def good_output(count=0, verdicts=None):
    verdicts = verdicts or {}
    return dict(equivalents=[dict(id=f"C{i}", verdict=verdicts.get(i, "incompatible"), reason="motivo")
                             for i in range(1, count + 1)],
                description="Mantém os alimentos frescos e a geladeira organizada",
                hashtags=["#PoteHermetico", "#CozinhaOrganizada"],
                hook="Close do pote sendo fechado com um clique", demo="Mão enche o pote e fecha a tampa",
                benefit="Alimentos frescos por mais tempo", cta="Pessoa aponta para o produto do vídeo",
                narration="Olha que prático: fecha com um clique e mantém tudo fresco. Toque no produto do vídeo.")


class FakeRunner:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.calls = output or good_output(), error, []
    def run(self, prompt, schema, images, check, log=None):
        self.calls.append(dict(prompt=prompt, images=images))
        if self.error:
            raise self.error
        output = self.output(images) if callable(self.output) else self.output
        assert check(output) == [], check(output)
        return output, "Fake", ["Fake: ok"]


def node(i=1, **overrides):
    value=dict(itemId=i,shopId=i,productName=f"Produto {i}",shopName=f"Loja {i}",
               offerLink=f"https://shopee.com.br/product/{i}/{i}",price="50",
               commission="5",appExistRate="0.10",appNewRate="0.30",
               ratingStar="4.8",sales=100,productCatIds=[i],periodStartTime=0,periodEndTime=0)
    value.update(overrides)
    return value


class RankingTest(unittest.TestCase):
    def test_conservative_commission(self):
        products, _, _=rank_offers([node()],DEFAULTS)
        self.assertEqual(products[0]["commission"],5)
    def test_duplicates_and_decisions_excluded(self):
        products,selected,_=rank_offers([node(),node(),node(2)],DEFAULTS,{"2:2"})
        self.assertEqual(len(products),1)
        self.assertEqual(selected,{"1:1"})
    def test_no_silent_rating_relaxation(self):
        with self.assertRaises(ValueError):
            rank_offers([node(ratingStar="4.1")],DEFAULTS)
        products,_,warning=rank_offers([node(ratingStar="4.1")],dict(DEFAULTS,relax_rating=True))
        self.assertEqual(len(products),1)
        self.assertIn("flexibilizada",warning)
    def test_categories_do_not_limit_products(self):
        _,selected,warning=rank_offers([node(i,productCatIds=[1]) for i in range(1,7)],DEFAULTS)
        self.assertEqual(len(selected),5)
        self.assertEqual(warning,"")
        self.assertNotIn("productCatId",shopee_query(DEFAULTS,1))
    def test_price_expiry_and_missing_expiry(self):
        products,_,_=rank_offers([node(),node(2,price="1000"),node(3,periodEndTime=1)],DEFAULTS)
        self.assertEqual(len(products),1)
    def test_invalid_settings(self):
        for payload in ({"min_rating":"garbage"},{"top_count":0},{"commission_weight":90},{"max_price":-1},{"relax_rating":"false"}):
            with self.assertRaises(ValueError):
                validate_settings(payload)
    @patch.dict(os.environ,{"SHOPEE_APP_ID":"123","SHOPEE_SECRET":"secret"})
    @patch("core.request_json")
    def test_success_without_errors_field(self,request):
        request.return_value={"data":{"productOfferV2":{"nodes":[node()],"pageInfo":{"hasNextPage":False}}}}
        self.assertEqual(len(fetch_offers(DEFAULTS)),1)
    @patch.dict(os.environ,{"SHOPEE_APP_ID":"123","SHOPEE_SECRET":"secret"})
    @patch("core.time.sleep")
    @patch("core.request_json")
    def test_temporary_failure_retries(self,request,sleep):
        request.side_effect=[TimeoutError(),{"data":{"productOfferV2":{"nodes":[node()],"pageInfo":{"hasNextPage":False}}}}]
        self.assertEqual(len(fetch_offers(DEFAULTS)),1)
        self.assertEqual(request.call_count,2)
    @patch.dict(os.environ,{"SHOPEE_APP_ID":"123","SHOPEE_SECRET":"secret"})
    @patch("core.request_json")
    def test_bounded_product_search(self,request):
        request.return_value={"data":{"productOfferV2":{"nodes":[node()],"pageInfo":{"hasNextPage":True}}}}
        self.assertEqual(len(fetch_offers(dict(DEFAULTS,max_pages=1))),1)
        self.assertEqual(request.call_count,1)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.store.save_settings(dict(schedule_enabled=True,auto_send=False))
        sleeper=patch("core.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.engine=Engine(self.store,fetcher=lambda _: [node(i) for i in range(1,7)],
                           searcher=lambda *args: [],runner=FakeRunner())
        self.store.save_telegram(dict(token="123456:"+("A"*30),chat_id="-1001234",thread_id="42"))

    def test_preview_does_not_send_and_history_persists(self):
        run_id=self.engine.collect()
        self.assertEqual(self.store.run(run_id)["delivery"],"not_sent")
        self.assertEqual(len(self.store.history("1:1")),1)
        self.assertEqual(Store(self.temp.name).runs()[0]["id"],run_id)

    def test_topic_payload_and_duplicate_delivery_guard(self):
        captured=[]
        self.engine.sender=lambda url,body: captured.append(json.loads(body)) or {"ok":True,"result":{"message_id":10}}
        run_id=self.engine.collect()
        self.engine.send(run_id)
        # One message per product plus its prompt as a reply; every one goes to the topic.
        self.assertEqual(len(captured),10)
        self.assertTrue(all(c["message_thread_id"]==42 and c["chat_id"]=="-1001234" for c in captured))
        self.assertTrue(all("reply_markup" not in c for c in captured))
        self.assertEqual(captured[1]["reply_parameters"]["message_id"],10)
        self.assertEqual(len(self.store.exclusions()),5)
        with self.assertRaises(ValueError):
            self.engine.send(run_id)
        self.assertEqual(len(captured),10)

    def test_two_previews_cannot_send_same_products(self):
        self.engine.sender=lambda url,body: {"ok":True,"result":{"message_id":10}}
        first=self.engine.collect()
        second=self.engine.collect()
        self.engine.send(first)
        with self.assertRaisesRegex(ValueError,"Nenhuma oferta"):
            self.engine.send(second)

    def test_old_sent_offers_never_return_to_selection(self):
        self.engine.sender=lambda url,body: {"ok":True,"result":{"message_id":10}}
        first=self.engine.collect()
        sent={p["key"] for p in self.store.run(first)["products"] if p["selected"]}
        self.engine.send(first)
        with self.store.db() as db:
            db.execute("UPDATE runs SET sent_at=? WHERE id=?",
                       ((datetime.now(timezone.utc)-timedelta(days=400)).isoformat(),first))
        self.store.save_settings({"repeat_days":0})
        second=self.engine.collect()
        found={p["key"] for p in self.store.run(second)["products"]}
        self.assertFalse(sent & found)
        self.assertEqual(len(found),1)

    def test_uncertain_delivery_is_not_recommended_again(self):
        self.engine.sender=lambda url,body: (_ for _ in ()).throw(TimeoutError())
        first=self.engine.collect()
        selected={p["key"] for p in self.store.run(first)["products"] if p["selected"]}
        with self.assertRaises(ValueError):
            self.engine.send(first)
        # Only the product whose message timed out is uncertain; the others were never attempted.
        uncertain={k for k,d in self.store.deliveries(first).items() if d["state"]=="unknown"}
        self.assertEqual(len(uncertain),1)
        second=self.engine.collect()
        found={p["key"] for p in self.store.run(second)["products"]}
        self.assertFalse(uncertain & found)
        self.assertEqual(len((selected-uncertain) & found),4)

    def test_unknown_delivery_never_retried(self):
        def fail(url,body):
            raise TimeoutError("timeout")
        self.engine.sender=fail
        run_id=self.engine.collect()
        with self.assertRaises(ValueError):
            self.engine.send(run_id)
        self.assertEqual(self.store.run(run_id)["delivery"],"unknown")
        with self.assertRaises(ValueError):
            self.engine.send(run_id)

    def test_telegram_token_not_returned(self):
        self.assertNotIn("token",self.store.telegram_public())
        token=self.store.telegram()["token"]
        self.store.save_telegram(dict(token="",chat_id="-1009999",thread_id=""))
        self.assertEqual(self.store.telegram()["token"],token)
        self.assertEqual(self.store.telegram()["thread_id"],"")

    def test_discarded_not_sent(self):
        captured=[]
        self.engine.sender=lambda u,b: captured.append(json.loads(b)) or {"ok":True,"result":{"message_id":1}}
        run_id=self.engine.collect()
        selected=next(p for p in self.store.run(run_id)["products"] if p["selected"])
        self.store.decision(selected["key"],"discarded")
        self.engine.send(run_id)
        self.assertFalse(any(selected["link"] in c["text"] for c in captured))

    def test_schedule_restarts_and_catches_up_once(self):
        now=datetime(2026,10,10,13,0,tzinfo=TZ)
        self.engine.tick(now)
        self.engine.tick(now+timedelta(minutes=2))
        self.assertEqual(len(self.store.runs()),1)
        restart=Engine(Store(self.temp.name),fetcher=lambda _: [node()])
        restart.tick(now+timedelta(minutes=3))
        self.assertEqual(len(self.store.runs()),1)

    def test_schedule_retry_does_not_wait_next_day(self):
        attempts=[]
        def flaky(_):
            attempts.append(1)
            if len(attempts)==1:
                raise ValueError("temporário")
            return [node()]
        self.engine.fetcher=flaky
        now=datetime(2026,10,10,13,0,tzinfo=TZ)
        self.engine.tick(now)
        self.engine.tick(now+timedelta(minutes=2))
        self.assertEqual(len(attempts),1)
        self.engine.tick(now+timedelta(minutes=6))
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.store.runs()[0]["status"],"ready")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ,{"PANEL_PASSWORD":"test-password-only"}):
            self.app=create_app(self.temp.name,testing=True)
        self.client=self.app.test_client()
        self.app.extensions["engine"].fetcher=lambda _: [node()]
        self.app.extensions["engine"].searcher=lambda *args: []
        self.app.extensions["engine"].runner=FakeRunner()
    def login(self):
        result=self.client.post("/api/login",json={"password":"test-password-only"})
        self.assertEqual(result.status_code,200)
        return {"X-CSRF-Token":result.json["csrf"]}
    def test_auth_and_csrf_required(self):
        self.assertEqual(self.client.get("/api/state").status_code,401)
        self.login()
        self.assertEqual(self.client.post("/api/settings",json={"keyword":"pets"}).status_code,403)
    def test_settings_and_telegram_secret_redaction(self):
        headers=self.login()
        result=self.client.post("/api/settings",json={"keyword":"pets"},headers=headers)
        self.assertEqual(result.status_code,200)
        token="123456:"+("B"*30)
        result=self.client.post("/api/telegram",json={"token":token,"chat_id":"-1001","thread_id":"12"},headers=headers)
        self.assertEqual(result.status_code,200)
        state=self.client.get("/api/state")
        self.assertNotIn(token,state.get_data(as_text=True))
        self.assertEqual(state.json["telegram"]["thread_id"],"12")

    def test_shopee_secret_never_returned(self):
        headers=self.login()
        secret="unit-test-shopee-secret"
        result=self.client.post("/api/shopee",json={"app_id":"123","secret":secret},headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertNotIn(secret,self.client.get("/api/state").get_data(as_text=True))
        self.assertTrue(self.client.get("/api/state").json["shopee_configured"])

    def test_cross_origin_and_invalid_json_rejected(self):
        headers=self.login()
        self.assertEqual(self.client.post("/api/settings",json={},headers={**headers,"Origin":"https://evil.test"}).status_code,403)
        self.assertEqual(self.client.post("/api/settings",data="bad",headers=headers).status_code,415)
    def test_preview_no_side_effect(self):
        self.login()
        run_id=self.app.extensions["engine"].collect()
        self.assertEqual(self.client.get(f"/api/runs/{run_id}/preview").status_code,200)
        self.assertEqual(self.app.extensions["store"].run(run_id)["delivery"],"not_sent")


if __name__=="__main__":
    unittest.main()
