import os
import time
import logging
from datetime import datetime, timedelta
import alpaca_trade_api as tradeapi
from typing import Dict, Optional
import json

class AlpacaTradingBot:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://paper-api.alpaca.markets"):
        """
        Initialize the trading bot
        
        Args:
            api_key: Alpaca API key
            api_secret: Alpaca API secret
            base_url: Alpaca API base URL (paper trading by default)
        """
        self.api = tradeapi.REST(api_key, api_secret, base_url, api_version='v2')
        
        # Trading parameters
        self.ticker = None
        self.initial_price = None
        self.current_price = None
        self.daily_high = None
        self.daily_low = None
        self.position_size = 0
        self.cash_reserve = 0  # The 5% we remove when stock goes up
        
        # Strategy parameters
        self.gain_threshold = 0.05  # 5% gain threshold
        self.loss_threshold = 0.10  # 10% loss threshold
        
        # Tracking
        self.trades_today = []
        self.is_running = False
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('trading_bot.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def get_account_info(self) -> Dict:
        """Get account information"""
        try:
            account = self.api.get_account()
            return {
                'buying_power': float(account.buying_power),
                'cash': float(account.cash),
                'portfolio_value': float(account.portfolio_value),
                'day_trade_count': int(account.day_trade_count)
            }
        except Exception as e:
            self.logger.error(f"Error getting account info: {e}")
            return {}

    def get_current_price(self, ticker: str) -> Optional[float]:
        """Get current price for ticker"""
        try:
            # Get latest trade
            latest_trade = self.api.get_latest_trade(ticker)
            return float(latest_trade.price)
        except Exception as e:
            self.logger.error(f"Error getting price for {ticker}: {e}")
            return None

    def get_position(self, ticker: str) -> Dict:
        """Get current position for ticker"""
        try:
            positions = self.api.list_positions()
            for position in positions:
                if position.symbol == ticker:
                    return {
                        'qty': int(position.qty),
                        'market_value': float(position.market_value),
                        'avg_entry_price': float(position.avg_entry_price),
                        'unrealized_pl': float(position.unrealized_pl),
                        'unrealized_plpc': float(position.unrealized_plpc)
                    }
            return {'qty': 0, 'market_value': 0, 'avg_entry_price': 0, 'unrealized_pl': 0, 'unrealized_plpc': 0}
        except Exception as e:
            self.logger.error(f"Error getting position for {ticker}: {e}")
            return {'qty': 0, 'market_value': 0, 'avg_entry_price': 0, 'unrealized_pl': 0, 'unrealized_plpc': 0}

    def place_order(self, ticker: str, qty: int, side: str) -> bool:
        """Place a market order"""
        try:
            order = self.api.submit_order(
                symbol=ticker,
                qty=abs(qty),
                side=side,
                type='market',
                time_in_force='day'
            )
            self.logger.info(f"Order placed: {side} {qty} shares of {ticker}")
            
            # Record the trade
            self.trades_today.append({
                'timestamp': datetime.now(),
                'ticker': ticker,
                'side': side,
                'qty': qty,
                'price': self.current_price,
                'reason': f"Rebalancing - {side}"
            })
            
            return True
        except Exception as e:
            self.logger.error(f"Error placing {side} order for {ticker}: {e}")
            return False

    def calculate_rebalance_action(self) -> Optional[Dict]:
        """
        Calculate if rebalancing is needed based on percentage changes
        
        Returns:
            Dict with action details or None if no action needed
        """
        if not self.initial_price or not self.current_price:
            return None
            
        # Calculate percentage change from initial price
        pct_change = (self.current_price - self.initial_price) / self.initial_price
        
        position = self.get_position(self.ticker)
        current_qty = position['qty']
        # Check for 5% gain threshold
        if pct_change >= self.gain_threshold and self.cash_reserve == 0:
            # Remove 5% of position (sell 5% of shares)
            if current_qty > 0:
                sell_qty = max(1, int(current_qty * 0.05))  # At least 1 share
                return {
                    'action': 'sell',
                    'qty': sell_qty,
                    'reason': f'5% gain reached ({pct_change:.2%}), removing 5% of position',
                    'reserve_cash': True
                }
        
        # Check for 10% loss threshold
        elif pct_change <= -self.loss_threshold and self.cash_reserve > 0:
            # Buy back with reserved cash
            if self.cash_reserve > 0 and self.current_price > 0:
                buy_qty = int(self.cash_reserve / self.current_price)
                if buy_qty > 0:
                    return {
                        'action': 'buy',
                        'qty': buy_qty,
                        'reason': f'10% loss reached ({pct_change:.2%}), investing reserved cash',
                        'use_reserve': True
                    }
        
        return None

    def execute_rebalance(self, action_dict: Dict) -> bool:
        """Execute the rebalancing action"""
        try:
            success = self.place_order(
                self.ticker,
                action_dict['qty'],
                action_dict['action']
            )
            
            if success:
                # Update cash reserve tracking
                if action_dict.get('reserve_cash'):
                    self.cash_reserve = action_dict['qty'] * self.current_price
                    self.logger.info(f"Reserved ${self.cash_reserve:.2f} in cash")
                
                elif action_dict.get('use_reserve'):
                    self.cash_reserve = 0
                    self.logger.info("Used reserved cash for purchase")
                
            return success
            
        except Exception as e:
            self.logger.error(f"Error executing rebalance: {e}")
            return False

    def is_market_open(self) -> bool:
        """Check if market is currently open"""
        try:
            clock = self.api.get_clock()
            return True #clock.is_open
        except Exception as e:
            self.logger.error(f"Error checking market status: {e}")
            return False

    def start_monitoring(self, ticker: str):
        """Start monitoring the ticker with the rebalancing strategy"""
        self.ticker = ticker.upper()
        self.logger.info(f"Starting monitoring for {self.ticker}")
        
        # Get initial price
        self.initial_price = self.get_current_price(self.ticker)
        if not self.initial_price:
            self.logger.error(f"Could not get initial price for {self.ticker}")
            return
        
        self.daily_high = self.initial_price
        self.daily_low = self.initial_price
        self.current_price = self.initial_price
        
        self.logger.info(f"Initial price for {self.ticker}: ${self.initial_price:.2f}")
        
        # Get initial position
        initial_position = self.get_position(self.ticker)
        self.logger.info(f"Initial position: {initial_position}")
        
        self.is_running = True
        
        try:
            while self.is_running and self.is_market_open():
                # Get current price
                current_price = self.get_current_price(self.ticker)
                if current_price:
                    self.current_price = current_price
                    
                    # Update daily high/low
                    self.daily_high = max(self.daily_high, current_price)
                    self.daily_low = min(self.daily_low, current_price)
                    
                    # Calculate percentage change
                    pct_change = (current_price - self.initial_price) / self.initial_price
                    
                    # Log current status
                    position = self.get_position(self.ticker)
                    from datetime import datetime
                    from zoneinfo import ZoneInfo   # Py3.9+
                    now_detroit = datetime.now(ZoneInfo("America/Detroit"))
                    time_now = now_detroit.strftime("%Y-%m-%d %H:%M:%S %Z")
                    self.logger.info(
                        f"Price: ${current_price:.2f} ({pct_change:+.2%}) | "
                        f"Position: {position['qty']} shares | "
                        f"Cash Reserve: ${self.cash_reserve:.2f} | "
                        f"timestamp: " + time_now
                    )
                    
                    # Check for rebalancing opportunity
                    action = self.calculate_rebalance_action()
                    if action:
                        self.logger.info(f"Rebalancing action: {action['reason']}")
                        success = self.execute_rebalance(action)
                        if success:
                            self.logger.info("Rebalancing executed successfully")
                        else:
                            self.logger.error("Rebalancing failed")
                
                # Wait 1 minute before next check
                time.sleep(30)
                
        except KeyboardInterrupt:
            self.logger.info("Monitoring stopped by user")
        except Exception as e:
            self.logger.error(f"Error in monitoring loop: {e}")
        
        self.stop_monitoring()

    def stop_monitoring(self):
        """Stop the monitoring process"""
        self.is_running = False
        self.logger.info("Monitoring stopped")
        
        # Print daily summary
        if self.ticker:
            self.print_daily_summary()

    def print_daily_summary(self):
        """Print summary of the day's activities"""
        print("\n" + "="*50)
        print(f"DAILY SUMMARY FOR {self.ticker}")
        print("="*50)
        
        if self.initial_price and self.current_price:
            total_change = (self.current_price - self.initial_price) / self.initial_price
            print(f"Initial Price: ${self.initial_price:.2f}")
            print(f"Final Price: ${self.current_price:.2f}")
            print(f"Daily Change: {total_change:+.2%}")
            print(f"Daily High: ${self.daily_high:.2f}")
            print(f"Daily Low: ${self.daily_low:.2f}")
        
        final_position = self.get_position(self.ticker)
        print(f"Final Position: {final_position['qty']} shares")
        print(f"Cash Reserve: ${self.cash_reserve:.2f}")
        
        print(f"\nTrades executed today: {len(self.trades_today)}")
        for trade in self.trades_today:
            print(f"  {trade['timestamp'].strftime('%H:%M:%S')} - "
                  f"{trade['side'].upper()} {trade['qty']} @ ${trade['price']:.2f} - "
                  f"{trade['reason']}")
        
        account_info = self.get_account_info()
        if account_info:
            print(f"\nAccount Value: ${account_info.get('portfolio_value', 0):.2f}")
            print(f"Buying Power: ${account_info.get('buying_power', 0):.2f}")

def main():
    """Main function to run the bot"""
    
    # You need to set these environment variables or replace with your actual keys
    API_KEY = os.getenv('ALPACA_API_KEY', 'your_api_key_here')
    API_SECRET = os.getenv('ALPACA_API_SECRET', 'your_api_secret_here')
    
    # Use paper trading URL for testing
    BASE_URL = 'https://paper-api.alpaca.markets'
    
    if API_KEY == 'your_api_key_here' or API_SECRET == 'your_api_secret_here':
        print("Please set your Alpaca API credentials!")
        print("Either set environment variables:")
        print("  export ALPACA_API_KEY='your_key'")
        print("  export ALPACA_API_SECRET='your_secret'")
        print("Or edit the script to include your credentials")
        return
    
    # Create bot instance
    bot = AlpacaTradingBot(API_KEY, API_SECRET, BASE_URL)
    
    # Get ticker from user
    ticker = input("Enter ticker symbol to monitor: ").strip().upper()
    if not ticker:
        print("No ticker provided!")
        return
    
    print(f"\nStarting monitoring for {ticker}")
    print("Strategy:")
    print("- When price gains 5% from daily open: Sell 5% of position")
    print("- When price falls 10% from daily open: Buy back with reserved cash")
    print("\nPress Ctrl+C to stop monitoring\n")
    
    # Start monitoring
    bot.start_monitoring(ticker)

if __name__ == "__main__":
    main()