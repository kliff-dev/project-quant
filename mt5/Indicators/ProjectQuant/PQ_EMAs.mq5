#property copyright "Project Quant local chart guide"
#property version "1.00"
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots 2
#property indicator_type1 DRAW_LINE
#property indicator_type2 DRAW_LINE
#property indicator_color1 clrDeepSkyBlue
#property indicator_color2 clrOrange
#property indicator_width1 2
#property indicator_width2 2

input int FastPeriod=20;
input int SlowPeriod=50;
input color FastColor=clrDeepSkyBlue;
input color SlowColor=clrOrange;
double Fast[],Slow[];

int OnInit()
{
   if(FastPeriod<1 || SlowPeriod<1) return INIT_PARAMETERS_INCORRECT;
   SetIndexBuffer(0,Fast,INDICATOR_DATA);
   SetIndexBuffer(1,Slow,INDICATOR_DATA);
   ArraySetAsSeries(Fast,false); ArraySetAsSeries(Slow,false);
   PlotIndexSetInteger(0,PLOT_LINE_COLOR,FastColor);
   PlotIndexSetInteger(1,PLOT_LINE_COLOR,SlowColor);
   PlotIndexSetInteger(0,PLOT_DRAW_BEGIN,FastPeriod-1);
   PlotIndexSetInteger(1,PLOT_DRAW_BEGIN,SlowPeriod-1);
   PlotIndexSetString(0,PLOT_LABEL,StringFormat("EMA %d",FastPeriod));
   PlotIndexSetString(1,PLOT_LABEL,StringFormat("EMA %d",SlowPeriod));
   IndicatorSetString(INDICATOR_SHORTNAME,StringFormat("PQ EMA %d / %d",FastPeriod,SlowPeriod));
   IndicatorSetInteger(INDICATOR_DIGITS,_Digits);
   return INIT_SUCCEEDED;
}

int OnCalculate(const int total,const int previous,const datetime &time[],
                const double &open[],const double &high[],const double &low[],const double &close[],
                const long &tick_volume[],const long &volume[],const int &spread[])
{
   if(total<1) return 0;
   ArraySetAsSeries(close,false);
   int start=1;
   if(previous<=0 || previous>total) { Fast[0]=close[0]; Slow[0]=close[0]; }
   else start=MathMax(1,previous-1);
   double a=2.0/(FastPeriod+1),b=2.0/(SlowPeriod+1);
   for(int i=start;i<total;i++)
   {
      Fast[i]=a*close[i]+(1-a)*Fast[i-1];
      Slow[i]=b*close[i]+(1-b)*Slow[i-1];
   }
   return total;
}
