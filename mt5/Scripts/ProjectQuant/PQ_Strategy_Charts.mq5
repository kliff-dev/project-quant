#property copyright "Project Quant local chart guide"
#property version "1.00"
#property description "Display-only: opens strategy charts, adds indicators and saves reusable templates."
#property description "No trading, account access, terminal settings, networking or external libraries."

void Label(const long id,const string name,const string text,const int y,const color ink,const int size=9)
{
   string key="PQ_"+name;
   // Recreate only our own labels so they draw above the existing guide panel.
   if(ObjectFind(id,key)>=0) ObjectDelete(id,key);
   ObjectCreate(id,key,OBJ_LABEL,0,0,0);
   ObjectSetInteger(id,key,OBJPROP_CORNER,CORNER_LEFT_UPPER);
   ObjectSetInteger(id,key,OBJPROP_XDISTANCE,12);
   ObjectSetInteger(id,key,OBJPROP_YDISTANCE,y);
   ObjectSetInteger(id,key,OBJPROP_COLOR,ink);
   ObjectSetInteger(id,key,OBJPROP_FONTSIZE,size);
   ObjectSetInteger(id,key,OBJPROP_SELECTABLE,false);
   ObjectSetString(id,key,OBJPROP_FONT,"Consolas");
   ObjectSetString(id,key,OBJPROP_TEXT,text);
}

bool Add(const long id,const int window,const int handle)
{
   if(handle==INVALID_HANDLE || !ChartIndicatorAdd(id,window,handle))
   {Print("Project Quant chart indicator failed: ",GetLastError());return false;}
   return true;
}

long Build(const string symbol,const ENUM_TIMEFRAMES period,const bool entry,const bool gold)
{
   // Re-running this display helper reuses its own chart instead of duplicating it.
   long id=0;
   for(long existing=ChartFirst();existing>=0;existing=ChartNext(existing))
      if(ChartSymbol(existing)==symbol && ChartPeriod(existing)==period && ObjectFind(existing,"PQ_TITLE")>=0)
         {id=existing;break;}
   bool fresh=(id==0);
   if(fresh) id=ChartOpen(symbol,period);
   if(id==0) {Print("Cannot open strategy chart ",symbol," ",EnumToString(period));return 0;}
   ChartSetInteger(id,CHART_MODE,CHART_CANDLES);
   ChartSetInteger(id,CHART_COLOR_BACKGROUND,C'14,21,32');
   ChartSetInteger(id,CHART_COLOR_FOREGROUND,clrSilver);
   ChartSetInteger(id,CHART_COLOR_GRID,C'32,40,54');
   ChartSetInteger(id,CHART_COLOR_CANDLE_BULL,C'48,190,145');
   ChartSetInteger(id,CHART_COLOR_CHART_UP,C'48,190,145');
   ChartSetInteger(id,CHART_COLOR_CANDLE_BEAR,C'238,98,104');
   ChartSetInteger(id,CHART_COLOR_CHART_DOWN,C'238,98,104');
   ChartSetInteger(id,CHART_SHOW_GRID,false);
   ChartSetInteger(id,CHART_SHOW_VOLUMES,0);
   ChartSetInteger(id,CHART_SHOW_TRADE_LEVELS,true);
   ChartSetInteger(id,CHART_AUTOSCROLL,true);
   ChartSetInteger(id,CHART_SHIFT,true);
   ChartSetDouble(id,CHART_SHIFT_SIZE,18);
   ChartSetInteger(id,CHART_SCALE,3);
   int fast=entry?20:50,slow=entry?50:200;
   color fast_color=entry?clrDeepSkyBlue:clrOrange,slow_color=entry?clrOrange:clrMediumPurple;
   bool ok=true;
   if(fresh)
   {
      ok=Add(id,0,iCustom(symbol,period,"ProjectQuant\\PQ_EMAs",fast,slow,fast_color,slow_color));
      int next=1;
      if(entry)
      {
         ok=Add(id,next,iCustom(symbol,period,"ProjectQuant\\PQ_RSI",14,gold?32.0:35.0,gold?68.0:65.0)) && ok;
         next++;
      }
      ok=Add(id,next,iCustom(symbol,period,"ProjectQuant\\PQ_ATR",14)) && ok;
   }
   if(ObjectFind(id,"PQ_PANEL")<0) ObjectCreate(id,"PQ_PANEL",OBJ_RECTANGLE_LABEL,0,0,0);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_XDISTANCE,8);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_YDISTANCE,18);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_XSIZE,490);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_YSIZE,92);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_BGCOLOR,C'14,21,32');
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_COLOR,C'54,68,88');
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_BORDER_TYPE,BORDER_FLAT);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_BACK,false);
   ObjectSetInteger(id,"PQ_PANEL",OBJPROP_SELECTABLE,false);
   string market=gold?"GOLD":"EUR/USD";
   string title=market+" | "+(entry?"ENTRY ":"TREND ")+StringSubstr(EnumToString(period),7);
   Label(id,"TITLE",title,22,clrWhite,11);
   Label(id,"LEGEND",entry?"EMA20 BLUE  |  EMA50 ORANGE":"EMA50 ORANGE  |  EMA200 PURPLE",42,clrSilver);
   if(entry)
   {
      Label(id,"RULE1","Closed candle reclaims EMA20 + stays beyond EMA50.",58,clrSilver);
      Label(id,"RULE2",gold?"RSI BUY 50-68 / SELL 32-50; confirm M15 trend.":"RSI BUY 50-65 / SELL 35-50; confirm H4 trend.",74,clrSilver);
   }
   else
   {
      Label(id,"RULE1","BUY: 50 > 200, close > 50, EMA50 rising.",58,clrSilver);
      Label(id,"RULE2","SELL: 50 < 200, close < 50, EMA50 falling.",74,clrSilver);
   }
   Label(id,"NOTE","Visual guide; Python monitor decides from CLOSED bars.",90,clrSlateGray,8);
   ChartRedraw(id);
   string filename=StringFormat("ProjectQuant_%s_%s.tpl",gold?"GOLD":"EURUSD",StringSubstr(EnumToString(period),7));
   if(!ChartSaveTemplate(id,filename)) Print("Template save failed: ",GetLastError());
   Print("Project Quant chart ",id," ",title," indicators_added=",ok," template=",filename);
   return id;
}

void OnStart()
{
   long gold_entry=Build("XAUUSDm",PERIOD_M5,true,true);
   long gold_context=Build("XAUUSDm",PERIOD_M15,false,true);
   long eur_entry=Build("EURUSDm",PERIOD_H1,true,false);
   long eur_context=Build("EURUSDm",PERIOD_H4,false,false);
   int file=FileOpen("ProjectQuant_Charts_setup.txt",FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(file!=INVALID_HANDLE)
   {
      FileWrite(file,"Display-only strategy charts; no order or account API calls.");
      long charts[4]; charts[0]=gold_entry;charts[1]=gold_context;charts[2]=eur_entry;charts[3]=eur_context;
      for(int i=0;i<4;i++)
      {
         long id=charts[i];
         FileWrite(file,StringFormat("chart=%I64d symbol=%s period=%s",id,ChartSymbol(id),EnumToString(ChartPeriod(id))));
         int windows=(int)ChartGetInteger(id,CHART_WINDOWS_TOTAL);
         for(int w=0;w<windows;w++)
            for(int j=0;j<ChartIndicatorsTotal(id,w);j++) FileWrite(file,"  ",ChartIndicatorName(id,w,j));
      }
      FileClose(file);
   }
   if(gold_entry>0) ChartSetInteger(gold_entry,CHART_BRING_TO_TOP,true);
   Print("Project Quant display setup finished. Trading remains controlled by Python.");
}
