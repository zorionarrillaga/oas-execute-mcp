//+------------------------------------------------------------------+
//|                                                  OAS_Bridge.mq5  |
//|                  Phase A.1 WebRequest HTTP EA                    |
//|                                                                  |
//| Wine MT5 blocks SocketConnect with ERR_FUNCTION_NOT_ALLOWED       |
//| (err=4014) even when the URL is in the WebRequest allowlist.      |
//| Fallback transport: HTTP via WebRequest(), which is the canonical |
//| Wine-compatible pattern. Same URL allowlist, two endpoints:        |
//|                                                                  |
//|   POST /poll   — EA asks for next queued command.                 |
//|                  Response body is either "op=empty\n" or the      |
//|                  full command (key=value lines).                  |
//|   POST /result — EA reports execution result.                     |
//|                  Body includes correlation_id so Python can       |
//|                  resolve the pending Future.                      |
//|                                                                  |
//| Heartbeat-file write kept as cheap liveness signal.               |
//+------------------------------------------------------------------+
#property copyright "oas-execute-mcp"
#property version   "3.10"
#property strict

#include <Trade\Trade.mqh>

input string ListenerBase      = "http://127.0.0.1:16275";  // matches WebRequest allowlist entry
input int    PollIntervalMs    = 100;                       // 100ms = 10 polls/sec
input int    HeartbeatMs       = 1000;                      // 1s heartbeat
input int    HttpTimeoutMs     = 2000;                      // per-request HTTP timeout
input int    DefaultDeviation  = 20;
input long   DefaultMagic      = 20260519;

CTrade  trade;
datetime g_last_heartbeat = 0;
string   g_bridge_dir = "oas_bridge";
ulong    g_poll_count = 0;
ulong    g_http_failures = 0;
ulong    g_last_failure_log = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetMillisecondTimer(PollIntervalMs);
   trade.SetExpertMagicNumber(DefaultMagic);
   trade.SetDeviationInPoints(DefaultDeviation);
   trade.SetTypeFillingBySymbol(_Symbol);
   WriteHeartbeat("init");
   PrintLog("OAS_Bridge v3.10 (WebRequest+fill_price_fallback) initialized; poll=" + IntegerToString(PollIntervalMs) +
            "ms base=" + ListenerBase + " magic=" + IntegerToString(DefaultMagic));
   return INIT_SUCCEEDED;
  }
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   PrintLog("OAS_Bridge deinit reason=" + IntegerToString(reason));
  }
//+------------------------------------------------------------------+
void OnTimer()
  {
   PollOnce();
   datetime now = TimeCurrent();
   if(now - g_last_heartbeat >= HeartbeatMs / 1000)
     {
      WriteHeartbeat("alive");
      g_last_heartbeat = now;
     }
  }
//+------------------------------------------------------------------+
//| One poll cycle: ask Python for a pending command, run it, report. |
//+------------------------------------------------------------------+
void PollOnce()
  {
   g_poll_count++;

   string poll_url = ListenerBase + "/poll";
   string body = "op=give_next_cmd\n";
   string response_text = "";
   int http_code = HttpPost(poll_url, body, response_text);
   if(http_code != 200)
     {
      g_http_failures++;
      ulong now_ms = GetTickCount64();
      if(now_ms - g_last_failure_log > 30000)
        {
         PrintLog("poll failed http=" + IntegerToString(http_code) +
                  " err=" + IntegerToString(GetLastError()) +
                  " failures=" + IntegerToString(g_http_failures));
         g_last_failure_log = now_ms;
        }
      return;
     }

   string op = ParseField(response_text, "op");
   if(op == "" || op == "empty") return;

   string correlation_id = ParseField(response_text, "correlation_id");
   if(correlation_id == "") correlation_id = "missing-id";

   PrintLog("PollOnce: op=" + op + " corr=" + correlation_id);

   string result = "";
   if     (op == "ping")           result = HandlePing(correlation_id);
   else if(op == "submit")         result = HandleSubmit(response_text, correlation_id);
   else if(op == "modify")         result = HandleModify(response_text, correlation_id);
   else if(op == "close")          result = HandleClose(response_text, correlation_id);
   else if(op == "close_all")      result = HandleCloseAll(response_text, correlation_id);
   else if(op == "list_positions") result = HandleListPositions(correlation_id);
   else if(op == "account_info")   result = HandleAccountInfo(correlation_id);
   else if(op == "quote")          result = HandleQuote(response_text, correlation_id);
   else                            result = "status=error\ncorrelation_id=" + correlation_id +
                                            "\nreason=unknown_op:" + op + "\n";

   string result_url = ListenerBase + "/result";
   string discard = "";
   int rc = HttpPost(result_url, result, discard);
   if(rc != 200)
      PrintLog("result POST failed http=" + IntegerToString(rc) +
               " err=" + IntegerToString(GetLastError()) +
               " corr=" + correlation_id);
  }
//+------------------------------------------------------------------+
//| HttpPost — wraps WebRequest. Returns HTTP status (0 on transport |
//| failure). On 200, fills `out_body` with the response body.        |
//+------------------------------------------------------------------+
int HttpPost(const string url, const string body, string &out_body)
  {
   out_body = "";
   char data[];
   StringToCharArray(body, data, 0, StringLen(body), CP_UTF8);

   string headers = "Content-Type: text/plain; charset=utf-8\r\n";
   string result_headers = "";
   char result[];

   ResetLastError();
   int code = WebRequest("POST", url, headers, HttpTimeoutMs, data, result, result_headers);
   if(code == -1) return 0;
   if(ArraySize(result) > 0)
      out_body = CharArrayToString(result, 0, ArraySize(result), CP_UTF8);
   return code;
  }
//+------------------------------------------------------------------+
//| Handlers                                                          |
//+------------------------------------------------------------------+
string HandlePing(const string correlation_id)
  {
   string r = "status=ok\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "ea_version=3.10\n";
   r += "server_time=" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\n";
   r += "account_login=" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n";
   r += "broker=" + AccountInfoString(ACCOUNT_COMPANY) + "\n";
   r += "server=" + AccountInfoString(ACCOUNT_SERVER) + "\n";
   r += "symbol_default=" + _Symbol + "\n";
   r += "poll_count=" + IntegerToString((long)g_poll_count) + "\n";
   return r;
  }
//+------------------------------------------------------------------+
//| HandleQuote — the BROKER's own live bid/ask for the #3 drift-guard.|
//| The charged-gun fire path must validate price-drift against the    |
//| price THIS broker will fill at (SymbolInfoDouble ASK/BID — same as |
//| HandleSubmit uses), NOT against a different feed (the Skilling      |
//| watcher), which has a basis offset. Returns symbol+bid+ask+mid+ts. |
//+------------------------------------------------------------------+
string HandleQuote(const string content, const string correlation_id)
  {
   string symbol = ParseField(content, "symbol");
   if(symbol == "") symbol = _Symbol;
   if(!SymbolSelect(symbol, true))
      return "status=error\ncorrelation_id=" + correlation_id +
             "\nreason=symbol_not_available:" + symbol + "\n";
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   if(bid <= 0 || ask <= 0)
      return "status=error\ncorrelation_id=" + correlation_id +
             "\nreason=no_quote_for_symbol:" + symbol + "\n";
   string r = "status=ok\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "symbol=" + symbol + "\n";
   r += "bid=" + DoubleToString(bid, _Digits) + "\n";
   r += "ask=" + DoubleToString(ask, _Digits) + "\n";
   r += "mid=" + DoubleToString((bid + ask) / 2.0, _Digits) + "\n";
   r += "server_time=" + IntegerToString((long)TimeCurrent()) + "\n";
   return r;
  }
//+------------------------------------------------------------------+
string HandleSubmit(const string content, const string correlation_id)
  {
   string symbol  = ParseField(content, "symbol");
   string side    = ParseField(content, "side");
   double lots    = StringToDouble(ParseField(content, "lots"));
   double sl      = StringToDouble(ParseField(content, "sl_price"));
   double tp      = StringToDouble(ParseField(content, "tp_price"));
   string dref    = ParseField(content, "decision_ref");

   if(symbol == "") symbol = _Symbol;
   if(!SymbolSelect(symbol, true))
      return Reject(correlation_id, "symbol_not_available:" + symbol);

   ENUM_ORDER_TYPE order_type = (side == "long") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double price = (side == "long") ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                                   : SymbolInfoDouble(symbol, SYMBOL_BID);
   if(price <= 0)
      return Reject(correlation_id, "no_quote_for_symbol:" + symbol);

   ulong t0 = GetTickCount64();
   bool ok = trade.PositionOpen(symbol, order_type, lots, price, sl, tp, dref);
   ulong latency = GetTickCount64() - t0;

   if(!ok)
     {
      string reason = "mt5_retcode=" + IntegerToString(trade.ResultRetcode()) +
                      " " + trade.ResultRetcodeDescription();
      return Reject(correlation_id, reason);
     }

   // ResultPrice() returns 0 on some brokers; fall back to the freshly-opened
   // position's POSITION_PRICE_OPEN, then to the deal's DEAL_PRICE in history.
   double real_fill = trade.ResultPrice();
   if(real_fill <= 0 && PositionSelectByTicket(trade.ResultOrder()))
      real_fill = PositionGetDouble(POSITION_PRICE_OPEN);
   if(real_fill <= 0)
     {
      ulong deal = trade.ResultDeal();
      if(deal > 0 && HistorySelect(TimeCurrent() - 60, TimeCurrent() + 60)
         && HistoryDealSelect(deal))
         real_fill = HistoryDealGetDouble(deal, DEAL_PRICE);
     }

   string r = "status=submitted\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "broker_order_id=" + IntegerToString(trade.ResultOrder()) + "\n";
   r += "deal_id=" + IntegerToString(trade.ResultDeal()) + "\n";
   r += "fill_price=" + DoubleToString(real_fill, _Digits) + "\n";
   r += "fill_time_iso=" + IsoNow() + "\n";
   r += "latency_ms=" + IntegerToString((int)latency) + "\n";
   return r;
  }
//+------------------------------------------------------------------+
string HandleModify(const string content, const string correlation_id)
  {
   ulong  ticket = (ulong)StringToInteger(ParseField(content, "broker_order_id"));
   string sl_s   = ParseField(content, "sl_price");
   string tp_s   = ParseField(content, "tp_price");

   if(!PositionSelectByTicket(ticket))
      return Reject(correlation_id, "broker_order_id_not_found:" + IntegerToString((long)ticket));

   double current_sl = PositionGetDouble(POSITION_SL);
   double current_tp = PositionGetDouble(POSITION_TP);
   double new_sl = (sl_s == "") ? current_sl : StringToDouble(sl_s);
   double new_tp = (tp_s == "") ? current_tp : StringToDouble(tp_s);

   ulong t0 = GetTickCount64();
   bool ok = trade.PositionModify(ticket, new_sl, new_tp);
   ulong latency = GetTickCount64() - t0;

   if(!ok)
      return Reject(correlation_id, "mt5_retcode=" + IntegerToString(trade.ResultRetcode()) +
                                    " " + trade.ResultRetcodeDescription());

   string r = "status=modified\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "broker_order_id=" + IntegerToString((long)ticket) + "\n";
   r += "sl_price=" + DoubleToString(new_sl, _Digits) + "\n";
   r += "tp_price=" + DoubleToString(new_tp, _Digits) + "\n";
   r += "latency_ms=" + IntegerToString((int)latency) + "\n";
   return r;
  }
//+------------------------------------------------------------------+
string HandleClose(const string content, const string correlation_id)
  {
   ulong  ticket = (ulong)StringToInteger(ParseField(content, "broker_order_id"));
   string lots_s = ParseField(content, "lots");

   if(!PositionSelectByTicket(ticket))
      return Reject(correlation_id, "broker_order_id_not_found:" + IntegerToString((long)ticket));

   double total_lots = PositionGetDouble(POSITION_VOLUME);
   double close_lots = (lots_s == "") ? total_lots : StringToDouble(lots_s);
   if(close_lots > total_lots) close_lots = total_lots;

   ulong t0 = GetTickCount64();
   bool ok;
   if(close_lots >= total_lots)
      ok = trade.PositionClose(ticket);
   else
      ok = trade.PositionClosePartial(ticket, close_lots);
   ulong latency = GetTickCount64() - t0;

   if(!ok)
      return Reject(correlation_id, "mt5_retcode=" + IntegerToString(trade.ResultRetcode()) +
                                    " " + trade.ResultRetcodeDescription());

   // Position is gone post-close, so query history for the closing deal price.
   double real_close = trade.ResultPrice();
   if(real_close <= 0)
     {
      ulong deal = trade.ResultDeal();
      if(deal > 0 && HistorySelect(TimeCurrent() - 60, TimeCurrent() + 60)
         && HistoryDealSelect(deal))
         real_close = HistoryDealGetDouble(deal, DEAL_PRICE);
     }

   string r = "status=closed\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "broker_order_id=" + IntegerToString((long)ticket) + "\n";
   r += "close_price=" + DoubleToString(real_close, _Digits) + "\n";
   r += "close_lots=" + DoubleToString(close_lots, 2) + "\n";
   r += "remaining_lots=" + DoubleToString(total_lots - close_lots, 2) + "\n";
   r += "latency_ms=" + IntegerToString((int)latency) + "\n";
   return r;
  }
//+------------------------------------------------------------------+
string HandleCloseAll(const string content, const string correlation_id)
  {
   string reason = ParseField(content, "reason");
   int total = PositionsTotal();
   int closed = 0;
   string details = "";
   for(int i = total - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(trade.PositionClose(ticket))
        {
         closed++;
         details += "closed:" + IntegerToString((long)ticket) + ";";
        }
      else
        {
         details += "failed:" + IntegerToString((long)ticket) +
                    "(" + IntegerToString(trade.ResultRetcode()) + ");";
        }
     }
   string r = "status=closed_all\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "reason=" + reason + "\n";
   r += "count=" + IntegerToString(closed) + "\n";
   r += "details=" + details + "\n";
   return r;
  }
//+------------------------------------------------------------------+
string HandleListPositions(const string correlation_id)
  {
   int total = PositionsTotal();
   string r = "status=ok\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "count=" + IntegerToString(total) + "\n";
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!PositionSelectByTicket(ticket)) continue;
      string idx = IntegerToString(i);
      string side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "long" : "short";
      r += "position." + idx + ".broker_order_id=" + IntegerToString((long)ticket) + "\n";
      r += "position." + idx + ".symbol=" + PositionGetString(POSITION_SYMBOL) + "\n";
      r += "position." + idx + ".side=" + side + "\n";
      r += "position." + idx + ".lots=" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + "\n";
      r += "position." + idx + ".entry_price=" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), _Digits) + "\n";
      r += "position." + idx + ".current_price=" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), _Digits) + "\n";
      r += "position." + idx + ".sl_price=" + DoubleToString(PositionGetDouble(POSITION_SL), _Digits) + "\n";
      r += "position." + idx + ".tp_price=" + DoubleToString(PositionGetDouble(POSITION_TP), _Digits) + "\n";
      r += "position." + idx + ".pnl=" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + "\n";
      r += "position." + idx + ".magic=" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) + "\n";
      r += "position." + idx + ".comment=" + PositionGetString(POSITION_COMMENT) + "\n";
     }
   return r;
  }
//+------------------------------------------------------------------+
string HandleAccountInfo(const string correlation_id)
  {
   string r = "status=ok\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\n";
   r += "equity=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\n";
   r += "margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + "\n";
   r += "free_margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + "\n";
   r += "profit=" + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2) + "\n";
   r += "broker=" + AccountInfoString(ACCOUNT_COMPANY) + "\n";
   r += "server=" + AccountInfoString(ACCOUNT_SERVER) + "\n";
   r += "currency=" + AccountInfoString(ACCOUNT_CURRENCY) + "\n";
   r += "account_login=" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n";
   r += "leverage=" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + "\n";
   r += "trade_allowed=" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false") + "\n";
   r += "expert_allowed=" + (AccountInfoInteger(ACCOUNT_TRADE_EXPERT) ? "true" : "false") + "\n";
   return r;
  }
//+------------------------------------------------------------------+
//| Heartbeat — file write, EA→Mac direction works fine on Wine       |
//+------------------------------------------------------------------+
void WriteHeartbeat(const string state)
  {
   string tmp_path   = g_bridge_dir + "\\heartbeat.txt.tmp";
   string final_path = g_bridge_dir + "\\heartbeat.txt";
   int fh = FileOpen(tmp_path, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if(fh == INVALID_HANDLE) return;
   FileWriteString(fh, "ts_iso=" + IsoNow() + "\nstate=" + state +
                       "\npoll_count=" + IntegerToString((long)g_poll_count) +
                       "\nhttp_failures=" + IntegerToString((long)g_http_failures) + "\n");
   FileClose(fh);
   FileDelete(final_path, FILE_COMMON);
   FileMove(tmp_path, FILE_COMMON, final_path, FILE_COMMON);
  }
//+------------------------------------------------------------------+
//| Helpers                                                           |
//+------------------------------------------------------------------+
string Reject(const string correlation_id, const string reason)
  {
   string r = "status=rejected\n";
   r += "correlation_id=" + correlation_id + "\n";
   r += "reason=" + reason + "\n";
   return r;
  }
//+------------------------------------------------------------------+
string ParseField(const string content, const string key)
  {
   string needle = key + "=";
   int idx = StringFind(content, needle);
   if(idx < 0) return "";
   if(idx > 0)
     {
      ushort prev = StringGetCharacter(content, idx - 1);
      if(prev != '\n' && prev != '\r') return "";
     }
   int start = idx + StringLen(needle);
   int eol1 = StringFind(content, "\n", start);
   int eol2 = StringFind(content, "\r", start);
   int eol = (eol1 < 0) ? eol2 : ((eol2 < 0) ? eol1 : MathMin(eol1, eol2));
   if(eol < 0) eol = StringLen(content);
   string val = StringSubstr(content, start, eol - start);
   StringTrimLeft(val);
   StringTrimRight(val);
   return val;
  }
//+------------------------------------------------------------------+
string IsoNow()
  {
   datetime t = TimeGMT();
   return TimeToString(t, TIME_DATE) + "T" + TimeToString(t, TIME_SECONDS) + "Z";
  }
//+------------------------------------------------------------------+
void PrintLog(const string msg)
  {
   Print("[OAS_Bridge] " + msg);
   int fh = FileOpen(g_bridge_dir + "\\bridge.log", FILE_WRITE|FILE_READ|FILE_TXT|FILE_COMMON|FILE_ANSI);
   if(fh != INVALID_HANDLE)
     {
      FileSeek(fh, 0, SEEK_END);
      FileWriteString(fh, IsoNow() + " " + msg + "\n");
      FileClose(fh);
     }
  }
//+------------------------------------------------------------------+
