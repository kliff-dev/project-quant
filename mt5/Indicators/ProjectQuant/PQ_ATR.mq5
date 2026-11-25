#property copyright "Project Quant local chart guide"
#property version "1.00"
#property indicator_separate_window
#property indicator_buffers 1
#property indicator_plots 1
#property indicator_type1 DRAW_LINE
#property indicator_color1 clrTurquoise
#property indicator_width1 1
#property indicator_minimum 0
input int Lookback=14;
double ATR[];

int OnInit()
{
   if(Lookback<1) return INIT_PARAMETERS_INCORRECT;
   SetIndexBuffer(0,ATR,INDICATOR_DATA);ArraySetAsSeries(ATR,false);
   PlotIndexSetInteger(0,PLOT_DRAW_BEGIN,Lookback-1);
   PlotIndexSetString(0,PLOT_LABEL,"Wilder ATR");
   IndicatorSetString(INDICATOR_SHORTNAME,StringFormat("PQ Wilder ATR %d | volatility, price units",Lookback));
   IndicatorSetInteger(INDICATOR_DIGITS,_Digits);
   return INIT_SUCCEEDED;
}

double Range(const int i,const double &high[],const double &low[],const double &close[])
{
   if(i==0) return high[0]-low[0];
   return MathMax(high[i]-low[i],MathMax(MathAbs(high[i]-close[i-1]),MathAbs(low[i]-close[i-1])));
}

int OnCalculate(const int total,const int previous,const datetime &time[],
                const double &open[],const double &high[],const double &low[],const double &close[],
                const long &tick_volume[],const long &volume[],const int &spread[])
{
   if(total<Lookback) return 0;
   ArraySetAsSeries(high,false);ArraySetAsSeries(low,false);ArraySetAsSeries(close,false);
   int start=Lookback;
   if(previous<=0 || previous>total)
   {
      double seed=0;
      for(int i=0;i<Lookback;i++) {seed+=Range(i,high,low,close);ATR[i]=EMPTY_VALUE;}
      ATR[Lookback-1]=seed/Lookback;
   }
   else start=MathMax(Lookback,previous-1);
   for(int i=start;i<total;i++) ATR[i]=((Lookback-1)*ATR[i-1]+Range(i,high,low,close))/Lookback;
   return total;
}
