#property copyright "Project Quant local chart guide"
#property version "1.00"
#property indicator_separate_window
#property indicator_buffers 3
#property indicator_plots 1
#property indicator_type1 DRAW_LINE
#property indicator_color1 clrMediumPurple
#property indicator_width1 2
#property indicator_minimum 0
#property indicator_maximum 100

input int Lookback=14;
input double SellFloor=32;
input double BuyCeiling=68;
double RSI[],Gain[],Loss[];

double Value(const double gain,const double loss)
{
   if(gain==0 && loss==0) return 50;
   if(loss==0) return 100;
   return 100-100/(1+gain/loss);
}

int OnInit()
{
   if(Lookback<1 || SellFloor<0 || SellFloor>=50 || BuyCeiling<=50 || BuyCeiling>100)
      return INIT_PARAMETERS_INCORRECT;
   SetIndexBuffer(0,RSI,INDICATOR_DATA);
   SetIndexBuffer(1,Gain,INDICATOR_CALCULATIONS);
   SetIndexBuffer(2,Loss,INDICATOR_CALCULATIONS);
   ArraySetAsSeries(RSI,false);ArraySetAsSeries(Gain,false);ArraySetAsSeries(Loss,false);
   PlotIndexSetInteger(0,PLOT_DRAW_BEGIN,Lookback);
   PlotIndexSetString(0,PLOT_LABEL,"Wilder RSI 14");
   IndicatorSetInteger(INDICATOR_LEVELS,3);
   IndicatorSetDouble(INDICATOR_LEVELVALUE,0,SellFloor);
   IndicatorSetDouble(INDICATOR_LEVELVALUE,1,50);
   IndicatorSetDouble(INDICATOR_LEVELVALUE,2,BuyCeiling);
   IndicatorSetInteger(INDICATOR_LEVELCOLOR,0,clrSlateGray);
   IndicatorSetInteger(INDICATOR_LEVELCOLOR,1,clrSilver);
   IndicatorSetInteger(INDICATOR_LEVELCOLOR,2,clrSlateGray);
   IndicatorSetInteger(INDICATOR_LEVELSTYLE,STYLE_DOT);
   IndicatorSetString(INDICATOR_SHORTNAME,StringFormat("PQ RSI %d | BUY 50-%.0f | SELL %.0f-50",Lookback,BuyCeiling,SellFloor));
   IndicatorSetInteger(INDICATOR_DIGITS,2);
   return INIT_SUCCEEDED;
}

int OnCalculate(const int total,const int previous,const datetime &time[],
                const double &open[],const double &high[],const double &low[],const double &close[],
                const long &tick_volume[],const long &volume[],const int &spread[])
{
   if(total<=Lookback) return 0;
   ArraySetAsSeries(close,false);
   int start=Lookback+1;
   if(previous<=0 || previous>total)
   {
      double gains=0,losses=0;
      for(int i=0;i<Lookback;i++) RSI[i]=EMPTY_VALUE;
      for(int i=1;i<=Lookback;i++)
      {
         double delta=close[i]-close[i-1];
         gains+=MathMax(delta,0);losses+=MathMax(-delta,0);
      }
      Gain[Lookback]=gains/Lookback;Loss[Lookback]=losses/Lookback;
      RSI[Lookback]=Value(Gain[Lookback],Loss[Lookback]);
   }
   else start=MathMax(Lookback+1,previous-1);
   for(int i=start;i<total;i++)
   {
      double delta=close[i]-close[i-1];
      Gain[i]=((Lookback-1)*Gain[i-1]+MathMax(delta,0))/Lookback;
      Loss[i]=((Lookback-1)*Loss[i-1]+MathMax(-delta,0))/Lookback;
      RSI[i]=Value(Gain[i],Loss[i]);
   }
   return total;
}
